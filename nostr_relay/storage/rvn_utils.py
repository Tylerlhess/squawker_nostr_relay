from ..config import Config
# TODO: test this to a call to the config. Import only if RVN is enabled and build server essentials into rvn_utils
from ravenrpc import Ravencoin
import ipfshttpclient
from ..util import (
    object_from_path,
    json_dumps,
    json_loads,
)
from aionostr.event import Event, EventKind


USER = Config.ravencoin["credentials"]["user"]
PASSWORD = Config.ravencoin["credentials"]["password"]
HOST = Config.ravencoin["rpc_host"]
PORT = Config.ravencoin["rpc_port"]

try:
    rvn = Ravencoin(USER, PASSWORD, host=HOST, port=PORT)
    rvn.getblockchaininfo()
except:
    if not rvn:
        rvn = None
    print("Ravnecoin is active but cannot connect to rpc node.")
    exit(1)

try:
    ipfs = ipfshttpclient.connect(Config.ravencoin["ipfs_host"])
except Exception as e:
    try:
        ipfs = ipfshttpclient.connect(Config.ravencoin["ipfs_host_fallback"])
    except Exception as f:
        print("Both IPFS hosts failed to connect.")
        exit(1)

ASSETNAMES = Config.ravencoin["asset_names"]
IPFSDIRPATH = Config.ravencoin["ipfs_dir_path"]
WALLET_ADDRESS = Config.ravencoin["wallet_address"]



import inspect


debug = 0

if debug:
    import logging

    logger = logging.getLogger('squawker_utils')
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(filename='squawker_utils.log', encoding='utf-8', mode='a')
    handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
    logger.addHandler(handler)
    handler2 = logging.FileHandler(filename='squawker.log', encoding='utf-8', mode='a')
    handler2.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
    logger.addHandler(handler2)
else:
    logger = 0


def ipfs_to_dict(ipfs_hash: str) -> dict:
    return json_loads(ipfs.cat(ipfs_hash))


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

def tx_to_self(tx:str, size:float=100.00):
    messages = dict()
    messages["addresses"] = [tx["address"]]
    messages["assetName"] = tx["assetName"]
    deltas = rvn.getaddressdeltas(messages)["result"]
    neg_delta = [(a["satoshis"], a["address"]) for a in deltas if a["txid"] == tx["txid"] and a["satoshis"] < -((size * 100000000)-1)]
    return len(neg_delta)


def find_latest_flags(asset:list=ASSETNAMES, satoshis:int=10000000000, count:int=50, pub_keys:list = None) -> list:
    try:
        deltas = []
        latest = []
        for _asset in asset:
            if logger: logger.info(f"asset is {_asset} satoshis {satoshis}")
            messages = dict()
            try:
                messages["addresses"] = list(rvn.listaddressesbyasset(_asset, False)["result"])
                if logger: logger.info(f"addresses {messages['addresses']}")
                messages["assetName"] = _asset
                prelim = rvn.getaddressdeltas(messages)
                if logger: logger.info(f"prelim = {prelim}")
                deltas.extend(prelim["result"])
            except IndexError:
                if logger: logger.info(f"*********************!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            except Exception as e:
                log_and_raise(e)
        for tx in deltas:
            if logger: logger.info(f"{int(str(tx['satoshis']))} - {int(str(satoshis))} = {int(str(tx['satoshis'])) - int(str(satoshis))}")
            if not (int(str(tx['satoshis'])) - int(str(satoshis))):
                # if logger: logger.info(f"tx = {tx}")
                # if logger: logger.info(f'{rvn.decoderawtransaction(rvn.getrawtransaction(tx["txid"])["result"])["result"]}')
                if tx_to_self(tx, size=(satoshis/100000000)):
                    # if logger: logger.info(f"tx is {type(tx)} {tx}")
                    transaction = rvn.decoderawtransaction(rvn.getrawtransaction(tx["txid"])["result"])["result"]
                    # if logger: logger.info(f"transaction is {transaction}")
                    for vout in transaction["vout"]:
                        vout = vout["scriptPubKey"]
                        if vout["type"] == "transfer_asset" and vout["asset"]["name"] in asset and vout["asset"]["amount"] == satoshis/100000000:
                            kaw = {
                                "address": vout["addresses"],
                                "message": vout["asset"]["message"],
                                "block": transaction["locktime"]
                            }
                            if pub_keys:
                                # Doing this for the logging
                                pub_key = ipfs_to_dict(kaw["message"])["pub_key"]
                                if pub_key in pub_keys:
                                    latest.append(kaw)
                                    if logger: logger.info(f"appended {kaw} with {pub_key=}")
                            else:
                                latest.append(kaw)
                                if logger: logger.info(f"appended {kaw}")
            # else:
            #     if logger: logger.info(f"transaction {tx} satoshis {tx['satoshis']} don't match {satoshis}")
        return sorted(latest[:count], key=lambda message: message["block"], reverse=True)
    except Exception as e:
        log_and_raise(e)


def transaction_scriptPubKey(tx_id:str, vout:str)->str:
    if logger: logger.info(f"Entered {inspect.stack()[0][3]} with {tx_id}, {vout}")
    if logger: logger.info(f"get raw transaction is {rvn.getrawtransaction(tx_id)['result']}")
    tx_data = rvn.decoderawtransaction(rvn.getrawtransaction(tx_id)['result'])['result']
    if logger: logger.info(f" decoded transaction data is {tx_data} looking for vout {vout}")
    issued_scriptPubKey = tx_data['vout'][vout]['scriptPubKey']['hex']
    return issued_scriptPubKey


def make_change(transaction:dict)->None:
    if logger: logger.info(f"making change of {transaction}")
    address = [key for key in transaction['outputs']][0]
    if logger: logger.info(f"address is {address}")
    asset = [key for key in transaction['outputs'][address]['transferwithmessage'] if key not in ["message", "expire_time"]][0]
    if logger: logger.info(f"asset is {asset}")
    amount_spent = transaction['outputs'][address]['transferwithmessage'][asset]
    if logger: logger.info(f"amount spent is {amount_spent}")
    raw_txs = []
    utxo_amount, rvn_amount = 0, 0
    asset_txs = []
    for tx in transaction['inputs']:
        is_asset, amount = find_input_value(tx)
        if is_asset:
            asset_txs.append(tx)
            utxo_amount += amount
        else:
            rvn_amount += amount

    if logger: logger.info(f"utxo amount = {utxo_amount}")
    if logger: logger.info(f"rvn amount = {rvn_amount}")
    change = utxo_amount - amount_spent
    if logger: logger.info(f"change = {change}")
    if logger: logger.info(f"setting rvn output amount to {rvn_amount} ")
    if change:
        transaction['outputs'][TEST_WALLET_ADDRESS] = {"transfer": {asset: change}}
        # TODO: move this function to raw transactions so ting.finance can fully support RPC calls.
        sendback = rvn.transferfromaddress({"asset": asset, "from_address": TEST_WALLET_ADDRESS, "amount": change, "to_address": address})
        if logger: logger.info(f"sendback results are {sendback}")
        if logger: logger.info(f"transaction = {transaction}")
    return # raw_txs


def find_input_value(tx:str)-> tuple[bool, str|int]:
    if logger: logger.info(f"handling {tx}")
    raw = rvn.getrawtransaction(tx['txid'])
    decoded = rvn.decoderawtransaction(raw['result'])
    details = decoded['result']['vout'][tx['vout']]
    if logger: logger.info(f" details = {details}")
    if details['value'] > 0:
        return False, details['value']
    if details['value'] == 0:
        return True, details['scriptPubKey']['asset']['amount']


def find_inputs(address, asset_quantity, current_asset):
    utxos = rvn.getaddressutxos({"addresses": [address], "assetName": current_asset})['result']
    tx = [txid for txid in utxos if txid['satoshis'] == asset_quantity]
    try:
        if len(tx) > 0:
            return [tx[0]]
        else:
            tx = [txid for txid in utxos if txid['satoshis'] > asset_quantity]
            if len(tx) == 0:
                txs = 0
                while txs < asset_quantity:
                    txs += utxos[0]['satoshis']
                    tx.append(utxos[0])
                    utxos = utxos[1:]
            return tx
    except IndexError as e:
        for tx in utxos:
            if tx['satoshis'] > 100000000:
                return [tx]
        total, txs = 0, []
        try:
            while total < asset_quantity:
                tx, utxos = utxos[0], utxos[1:]
                total += tx['satoshis']
                txs.append(tx)
        except IndexError:
            raise Exception(f"Ran out of assets {utxos}")
        return txs

def log_and_raise(error:Exception)->Exception:
    if logger: logger.info(f"Exception {type(error)} {str(error)}")
    raise error