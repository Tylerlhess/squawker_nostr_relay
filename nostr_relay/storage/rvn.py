"""This class of storage extends upon the DBstorage Class.
It uses postgres as a local cache while also recording events on the Ravencoin 
Blockchain using the Squawker protocol. 
This adds a layer of complexity by needing access to a Ravencoin node to query
for events others have recorded. To record events you will likely need to run
your own Ravencoin node and get access to some assets or tokens used for
recording events. Each recorded event will also have a Ravencoin transaction 
cost which currently is many fractions of a penny."""


from rvnserver import *
import asyncio
import logging
from datetime import datetime
from time import time

import sqlalchemy as sa
from sqlalchemy.engine.base import Engine

from aionostr.event import Event, EventKind
from ..config import Config
from ..auth import Action
from ..errors import StorageError, AuthenticationError
from ..util import (
    object_from_path,
    json_dumps,
    json_loads,
)
from . import get_metadata
from .base import BaseSubscription, BaseGarbageCollector, NostrQuery
from .db import DBStorage, event_from_tuple, validate_id


force_hex_translation = str.maketrans(
    "abcdef0213456789",
    "abcdef0213456789",
    "ghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
)

def ipfs_to_dict(ipfs_file_hash):
    return json_loads(ipfs.cat(ipfs_file_hash))
    # return f'["EVENT","{sub_id}",{{"id":"{event.id}","created_at":{event.created_at},"pubkey":"{event.pubkey}","kind":{event.kind},"sig":"{event.sig}","content":{encode_basestring(event.content)},"tags":[{tags}]}}]'


def event_from_blockchain(ipfs_file_hash):
    # TODO: handle events coming from the blockchain 
    jd = ipfs_to_dict(ipfs_file_hash)

    # ##### create event
    # def __init__(
    #         self,
    #         pubkey: str='', 
    #         content: str='', 
    #         created_at: int=0, 
    #         kind: int=EventKind.TEXT_NOTE, 
    #         tags: "list[list[str]]"=[], 
    #         id: str=None,
    #         sig: str=None) -> None:
    
    return Event(
        id=jd["id"].hex(),
        created_at=jd['created_at'],
        kind=jd["kind"],
        pubkey=jd["pubkey"].hex(),
        tags=jd["tags"],
        sig=jd["sig"].hex(),
        content=jd["content"],
    )

def event_from_tuple(row):
    # TODO: handle events coming from the blockchain 
    tags = row[4]
    if isinstance(tags, str):
        tags = json_loads(tags)
    return Event(
        id=row[0].hex(),
        created_at=row[1],
        kind=row[2],
        pubkey=row[3].hex(),
        tags=tags,
        sig=row[5].hex(),
        content=row[6],
    )


class RVNStorage(DBStorage):
    
    def __init__(self, options):
        self.options, self.rvn = self.parse_rvn_options(options)
        super().__init__(options)
        try:
            self.rpc_port = self.rvn["rpc_port"]
        except:
            self.rpc_port = 8766

    def parse_rvn_options(self, options):
        storage_options, rvn_options = {}, {}
        for key, value in options.items():
            if key.startswith("rvn."):
                rvn_options[key.replace("rvn.", "")] = value
            else:
                storage_options[key] = value
        return storage_options, rvn_options

    async def setup(self):
        await super().setup()

        self.tx_slot = asyncio.Semaphore(
            int(self.options.pop("num_concurrent_txs", 4))
        )
        # This must be set below the number of possible tokens required to transact 
        # if you are doing events upto 100 and you have 1000 tokens you need to have this below 10.
        self.log.info("Connected to %s", self.rpc_port)

        metadata = super().get_metadata()
        self.EventTable = metadata.tables["events"]
        self.IdentTable = metadata.tables["identity"]
        self.AuthTable = metadata.tables["auth"]
        TagTable = metadata.tables["tags"]
        
        self.log.debug("done setting up")

    async def get_event(self, event_id):
        """
        Shortcut for retrieving an event by id
        """
        async with self.query_slot:
            async with self.db.connect() as conn:
                result = await conn.execute(
                    sa.select(self.EventTable).where(
                        self.EventTable.c.id == bytes.fromhex(event_id)
                    )
                )
                row = result.first()
        if not row:
            result = await self.query_blockchain(self.tokens, event_id)
            result_event = event_from_blockchain(result)
            if result_event.kind == 1:
                self.add_event(result)
            return result_event
        else: 
            return event_from_tuple(row)
        
    async def query_blockchain(tokens, event_id):
        # TODO: querying the blockchain
        pass

    async def write_event_to_blockchain(event, asset=ASSETNAME):
        # TODO: write to the blockchain requires rav_utils to handle server creation.
        pass


    async def add_event(self, event_json, auth_token=None):
        """
        Add an event from json object
        Return (status, event)
        """
        try:
            event = Event(**event_json)
        except Exception:
            self.log.error("bad json")
            raise StorageError("invalid: Bad JSON")

        await self.validate_event(event, Config)
        # check authentication
        if not await self.authenticator.can_do(auth_token, Action.save.value, event):
            raise AuthenticationError("restricted: permission denied")

        changed = False
        with self.stat_collector.timeit("insert") as counter:
            async with self.add_slot:
                async with self.db.begin() as conn:
                    do_save = await self.pre_save(conn, event)
                    if do_save:
                        result = await conn.execute(
                            self.event_insert_query.values(
                                id=event.id_bytes,
                                created_at=event.created_at,
                                pubkey=bytes.fromhex(event.pubkey),
                                sig=bytes.fromhex(event.sig),
                                content=event.content,
                                kind=event.kind,
                                tags=event.tags,
                            )
                        )
                        changed = result.rowcount == 1
                        await self.post_save(event, connection=conn, changed=changed)
            counter["count"] += 1
        if changed:
            try:
                await self.write_event_to_blockchain(event)
            except:
                self.log.error(f"failed to write to blockchain event id {event.id}")
                # TODO: Do I want to raise an exception here?
                # Maybe check for types of failure events - No funds, RPC failed, others
            await self.notify_all_connected(event)
            # notify other processes
            await self.notify_other_processes(event)
        return event, changed

    async def process_tags(self, conn, event):
        if event.tags:
            # update mentions
            # single-letter tags can be searched
            # delegation tags are also searched
            # expiration tags are also added for the garbage collector
            tags = set()
            for tag in event.tags:
                if tag[0] in ("delegation", "expiration"):
                    tags.add((tag[0], tag[1]))
                elif len(tag[0]) == 1:
                    tags.add((tag[0], tag[1] if len(tag) > 1 else ""))
            if tags:
                await conn.execute(
                    self.tag_insert_query,
                    [
                        {"id": event.id_bytes, "name": tag[0], "value": tag[1]}
                        for tag in tags
                    ],
                )

            if event.kind == EventKind.DELETE:
                # delete the referenced events
                for tag in event.tags:
                    name = tag[0]
                    if name == "e":
                        event_id = tag[1]
                        query = sa.delete(self.EventTable).where(
                            (self.EventTable.c.pubkey == bytes.fromhex(event.pubkey))
                            & (self.EventTable.c.id == bytes.fromhex(event_id))
                        )
                        await conn.execute(query)
                        self.log.info("Deleted event %s", event_id)

    async def post_save(self, event, connection=None, changed=None):
        """
        Post-process event
        (clear old metadata, update tag references)
        """

        if changed:
            if event.kind in (EventKind.SET_METADATA, EventKind.CONTACTS):
                # older metadata events can be cleared
                await connection.execute(
                    self.EventTable.delete().where(
                        (self.EventTable.c.pubkey == bytes.fromhex(event.pubkey))
                        & (self.EventTable.c.kind == event.kind)
                        & (self.EventTable.c.created_at < event.created_at)
                    )
                )
            await self.process_tags(connection, event)

    async def run_single_query(self, query_filters):
        """
        Run a single query, yielding json events
        """
        nostr_queries = [NostrQuery.model_validate(q) for q in query_filters]
        queue = asyncio.Queue()
        sub = self.subscription_class(
            self, "", nostr_queries, queue=queue, default_limit=600000
        )
        if sub.prepare():
            async for event in self.run_query(sub.query):
                yield event

    async def run_query(self, query, if_long=None):
        self.log.debug(query)
        try:
            with self.stat_collector.timeit("query") as counter:
                async with self.query_slot:
                    async with self.db.connect() as conn:
                        async with conn.stream(query) as result:
                            async for row in result:
                                yield event_from_tuple(row)
                                counter["count"] += 1
            duration = counter["duration"]
            if duration > 1.0 and if_long:
                if_long(duration)
        except Exception:
            self.log.exception("subscription")

    async def get_stats(self):
        stats = {"total": 0}
        async with self.db.connect() as conn:
            result = await conn.stream(
                sa.text(
                    "SELECT kind, COUNT(*) FROM events GROUP BY kind order by 2 DESC"
                )
            )
            kinds = {}
            async for kind, count in result:
                kinds[kind] = count
                stats["total"] += count
            stats["kinds"] = kinds

            if self.is_postgres:
                result = await conn.execute(
                    sa.text(
                        """
                        SELECT
                            SUM(pg_total_relation_size(table_name ::text))
                        FROM (
                            -- tables from 'public'
                            SELECT table_name
                            FROM information_schema.tables
                            where table_schema = 'public' and table_type = 'BASE TABLE'
                        ) AS all_tables
                """
                    )
                )
                row = result.first()
                stats["db_size"] = int(row[0])
            else:
                try:
                    result = await conn.execute(
                        sa.text(
                            'SELECT SUM("pgsize") FROM "dbstat" WHERE name in ("events", "tags")'
                        )
                    )
                    row = result.first()
                    stats["db_size"] = row[0]
                except (sa.exc.OperationalError, sa.exc.ProgrammingError):
                    pass
        subs = await self.num_subscriptions(True)
        num_subs = 0
        num_clients = 0
        for k, v in subs.items():
            num_clients += 1
            num_subs += v
        stats["active_subscriptions"] = num_subs
        stats["active_clients"] = num_clients
        return stats

    async def get_identified_pubkey(self, identifier, domain=""):
        query = sa.select(
            self.IdentTable.c.pubkey,
            self.IdentTable.c.identifier,
            self.IdentTable.c.relays,
        )
        if domain:
            query = query.where(self.IdentTable.c.identifier.like(f"%@{domain}"))
        if identifier:
            query = query.where(self.IdentTable.c.identifier == identifier)
        data = {"names": {}, "relays": {}}
        self.log.debug("Getting identity for %s %s", identifier, domain)
        async with self.query_slot:
            async with self.db.connect() as conn:
                result = await conn.stream(query)
                async for pubkey, identifier, relays in result:
                    data["names"][identifier.split("@")[0]] = pubkey
                    if relays:
                        data["relays"][pubkey] = relays

        return data

    async def set_identified_pubkey(self, identifier, pubkey, relays=None):
        async with self.db.begin() as conn:
            if not pubkey:
                await conn.execute(
                    self.IdentTable.delete().where(
                        self.IdentTable.c.identifier == identifier
                    )
                )
            elif not (validate_id(pubkey) and len(pubkey) == 64):
                raise StorageError("invalid public key")
            else:
                [identifier, pubkey, json_dumps(relays or [])]
                await conn.execute(
                    sa.delete(self.IdentTable).where(
                        self.IdentTable.c.identifier == identifier
                    )
                )
                stmt = sa.insert(self.IdentTable).values(
                    {"identifier": identifier, "pubkey": pubkey, "relays": relays}
                )
                await conn.execute(stmt)

    async def get_auth_roles(self, pubkey):
        """
        Get the roles assigned to the public key
        """
        async with self.db.begin() as conn:
            result = await conn.execute(
                sa.select(self.AuthTable.c.roles).where(
                    self.AuthTable.c.pubkey == pubkey
                )
            )
            row = result.fetchone()
        if row:
            return set(row[0].lower())
        else:
            return self.authenticator.default_roles

    async def get_all_auth_roles(self):
        """
        Return all roles in authentication table
        """
        async with self.db.begin() as conn:
            result = await conn.stream(
                sa.select(self.AuthTable.c.pubkey, self.AuthTable.c.roles)
            )
            async for pubkey, role in result:
                yield pubkey, set((role or "").lower())

    async def set_auth_roles(self, pubkey: str, roles: str):
        """
        Assign roles to the given public key
        """
        async with self.db.begin() as conn:
            try:
                await conn.execute(
                    sa.insert(self.AuthTable).values(
                        pubkey=pubkey, roles=roles, created=datetime.now()
                    )
                )
            except sa.exc.IntegrityError:
                await conn.execute(
                    sa.update(self.AuthTable)
                    .where(self.AuthTable.c.pubkey == pubkey)
                    .values(roles=roles)
                )


class Subscription(BaseSubscription):
    __slots__ = ("is_postgres",)

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.is_postgres = self.storage.is_postgres

    def prepare(self):
        try:
            self.query, self.filters = self.build_query(self.filters)
        except Exception:
            self.log.exception("build_query")
            return False
        return True

    async def run_query(self):
        sub_id = self.sub_id
        queue = self.queue
        check_output = self.storage.check_output

        results = self.storage.run_query(
            self.query,
            if_long=lambda duration: logging.getLogger(
                "nostr_relay.long-queries"
            ).warning(
                f"{self.client_id}/{self.sub_id} Long query: '{self.filters}' took %dms",
                duration * 1000,
            ),
        )
        if check_output:
            context = {
                "config": Config,
                "client_id": self.client_id,
                "auth_token": self.auth_token,
            }
            async for event in results:
                if check_output(event, context):
                    await queue.put((sub_id, event))
        else:
            async for event in results:
                await queue.put((sub_id, event))
        await queue.put((sub_id, None))

    def evaluate_filter(self, filter_obj, subwhere):
        if filter_obj.ids is not None:
            if filter_obj.ids:
                exact = []
                for eid in filter_obj.ids:
                    if len(eid) == 64:
                        if self.is_postgres:
                            exact.append(f"'\\x{eid}'")
                        else:
                            exact.append(f"x'{eid}'")
                    elif len(eid) > 2:
                        if self.is_postgres:
                            subwhere.append(f"encode(id, 'hex') LIKE '{eid}%'")
                        else:
                            subwhere.append(f"lower(hex(id)) LIKE '{eid}%'")
                if exact:
                    idstr = ",".join(exact)
                    subwhere.append(f"events.id IN ({idstr})")
            else:
                raise ValueError("ids")
        if filter_obj.authors is not None:
            if filter_obj.authors:
                exact = set()
                hexexact = set()
                for pubkey in filter_obj.authors:
                    if len(pubkey) == 64:
                        if self.is_postgres:
                            exact.add(f"'\\x{pubkey}'")
                        else:
                            exact.add(f"x'{pubkey}'")
                        hexexact.add(f"'{pubkey}'")
                        # no prefix searches, for now
                if exact:
                    astr = ",".join(exact)
                    subwhere.append(
                        f"(pubkey IN ({astr}) OR id IN (SELECT id FROM tags WHERE name = 'delegation' AND value IN ({','.join(hexexact)})))"
                    )
                else:
                    raise ValueError("authors")
            else:
                # query with empty list should be invalid
                raise ValueError("authors")
        if filter_obj.kinds is not None:
            if filter_obj.kinds:
                subwhere.append(
                    "kind IN ({})".format(",".join(str(k) for k in filter_obj.kinds))
                )
            else:
                raise ValueError("kinds")
        if filter_obj.since is not None:
            subwhere.append("created_at >= %d" % filter_obj.since)
        if filter_obj.until is not None:
            subwhere.append("created_at < %d" % filter_obj.until)
        if filter_obj.tags:
            for tagname, tags in filter_obj.tags:
                pstr = []
                for val in tags:
                    if val:
                        val = val.replace("'", "''")
                        pstr.append(f"'{val}'")
                if pstr:
                    pstr = ",".join(pstr)
                    subwhere.append(
                        f"id IN (SELECT id FROM tags WHERE name = '{tagname}' AND value IN ({pstr})) "
                    )
        return filter_obj

    def build_query(self, filters):
        select = """
            SELECT id, created_at, kind, pubkey, tags, sig, content FROM events
        """
        where = set()
        limit = None
        new_filters = []
        for filter_obj in filters:
            subwhere = []
            try:
                filter_obj = self.evaluate_filter(filter_obj, subwhere)
            except ValueError:
                self.log.debug("bad query %s", filter_obj)
                filter_obj = NostrQuery()
                subwhere = []
            if subwhere:
                subwhere = " AND ".join(subwhere)
                where.add(subwhere)
            else:
                where.add("false")
            if filter_obj.limit:
                limit = min(filter_obj.limit, self.default_limit)
            new_filters.append(filter_obj)
        if where:
            select += " WHERE (\n\t"
            select += "\n) OR (\n".join(where)
            select += ")"
        if limit is None:
            limit = self.default_limit
        select += f"""
            ORDER BY created_at DESC
            LIMIT {limit}
        """
        return sa.text(select), new_filters


class QueryGarbageCollector(BaseGarbageCollector):
    query = """
        DELETE FROM events WHERE events.id IN
        (
            SELECT events.id FROM events
            LEFT JOIN tags on tags.id = events.id
            WHERE 
                (kind >= 20000 and kind < 30000)
            OR
                (tags.name = 'expiration' AND tags.value < '%NOW%')
        )
    """

    async def collect(self, conn):
        result = await conn.execute(
            sa.text(self.query.replace("%NOW%", str(int(time()))))
        )
        return max(0, result.rowcount)
