
# TODO: convert this to a call to the config. Import only if RVN is enabled and build server essentials into rvn_utils
from ServerEssentials.serverside import *
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



def tx_to_self(tx, size=1.00):
    messages = dict()
    messages["addresses"] = [tx["address"]]
    messages["assetName"] = tx["assetName"]
    deltas = rvn.getaddressdeltas(messages)["result"]
    neg_delta = [(a["satoshis"], a["address"]) for a in deltas if a["txid"] == tx["txid"] and a["satoshis"] < -((size * 100000000)-1)]
    return len(neg_delta)


def find_latest_flags(asset=ASSETNAME, satoshis=100000000, count=50):
    try:
        latest = []
        if logger: logger.info(f"asset is {asset} satoshis {satoshis}")
        messages = dict()
        try:
            messages["addresses"] = list(rvn.listaddressesbyasset(asset, False)["result"])
            if logger: logger.info(f"addresses {messages['addresses']}")
            messages["assetName"] = asset
            prelim = rvn.getaddressdeltas(messages)
            if logger: logger.info(f"prelim = {prelim}")
            deltas = prelim["result"]
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
                        if vout["type"] == "transfer_asset" and vout["asset"]["name"] == asset and vout["asset"]["amount"] == satoshis/100000000:
                            kaw = {
                                "address": vout["addresses"],
                                "message": vout["asset"]["message"],
                                "block": transaction["locktime"]
                            }
                            latest.append(kaw)
                            if logger: logger.info(f"appended {kaw}")
            # else:
            #     if logger: logger.info(f"transaction {tx} satoshis {tx['satoshis']} don't match {satoshis}")
        return sorted(latest[:count], key=lambda message: message["block"], reverse=True)
    except Exception as e:
        log_and_raise(e)


def transaction_scriptPubKey(tx_id, vout):
    if logger: logger.info(f"Entered {inspect.stack()[0][3]} with {tx_id}, {vout}")
    if logger: logger.info(f"get raw transaction is {rvn.getrawtransaction(tx_id)['result']}")
    tx_data = rvn.decoderawtransaction(rvn.getrawtransaction(tx_id)['result'])['result']
    if logger: logger.info(f" decoded transaction data is {tx_data} looking for vout {vout}")
    issued_scriptPubKey = tx_data['vout'][vout]['scriptPubKey']['hex']
    return issued_scriptPubKey


def make_change(transaction):
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
        sendback = rvn.transferfromaddress({"asset": asset, "from_address": TEST_WALLET_ADDRESS, "amount": change, "to_address": address})
        if logger: logger.info(f"sendback results are {sendback}")
        if logger: logger.info(f"transaction = {transaction}")
    return # raw_txs


def find_input_value(tx):
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

def log_and_raise(error):
    if logger: logger.info(f"Exception {type(error)} {str(error)}")
    raise error