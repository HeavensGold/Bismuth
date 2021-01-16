"""
API Command handler module for Bismuth nodes
@EggPoolNet
Needed for Json-RPC server or other third party interaction
"""

import base64
import json
import os
import sys
import threading
from typing import TYPE_CHECKING

import libs.mempool as mp
from bismuthcore.transaction import Transaction
# modular handlers will need access to the database methods under some form, so it needs to be modular too.
# Here, I just duplicated the minimum needed code from node, further refactoring with classes will follow.
from libs import connections
from polysign.signerfactory import SignerFactory

# from essentials import format_raw_tx
if TYPE_CHECKING:
    from libs.node import Node
    from libs.dbhandler import DbHandler

__version__ = "0.0.15"


class ApiHandler:
    """
    The API commands manager. Extra commands, not needed for node communication, but for third party tools.
    Handles all commands prefixed by "api_".
    It's called from client threads, so it has to be thread safe.
    """

    __slots__ = ('app_log', 'config', 'callback_lock', 'callbacks')

    def __init__(self, node: "Node"):
        self.app_log = node.logger.app_log
        self.config = node.config
        # Avoid mixing answers to commands with callbacks
        self.callback_lock = threading.Lock()
        # list of sockets that asked for a callback (new block notification)
        # Not used yet.
        self.callbacks = []

    def dispatch(self, method, socket_handler, db_handler: "DbHandler", peers):
        """
        Routes the call to the right method
        :return:
        """
        # Easier to ask forgiveness than ask permission
        try:
            """
            All API methods share the same interface. Not storing in properties since it has to be thread safe.
            This is not pretty, this will evolve with more modular code.
            Primary goal is to limit the changes in node.py code and allow more flexibility in this class, like plugin.
            """
            call = getattr(self, method)
            if call is None:
                self.app_log.warning(f"API Method <{method}> does not exist.")
                return False
            result = call(socket_handler, db_handler, peers)
            return result
        except Exception as e:
            # raise
            self.app_log.warning(f"Exception calling method {method}: {e}")
            return False

    def blocktojsondiffs(self, list_of_txs: list, list_of_diffs: list) -> dict:
        """Beware, returns a dict, not a json encoded payload"""
        i = 0
        blocks_dict = {}
        block_dict = {}
        normal_transactions = []

        old = None

        for transaction in list_of_txs:
            # EGG_EVO: Is decode_pubkey needed? Where is that used?
            transaction_formatted = Transaction.from_legacy(transaction).to_dict(legacy=True, decode_pubkey=True)
            height = transaction_formatted["block_height"]

            del transaction_formatted["block_height"]
            #  del transaction_formatted["signature"]  # optional
            #  del transaction_formatted["pubkey"]  # optional

            if old != height:
                block_dict.clear()
                del normal_transactions[:]

            if transaction_formatted["reward"] == "0.00000000":  # if normal tx
                del transaction_formatted["block_hash"]
                del transaction_formatted["reward"]
                normal_transactions.append(transaction_formatted)

            else:
                del transaction_formatted["address"]
                del transaction_formatted["amount"]

                transaction_formatted['difficulty'] = list_of_diffs[i][0]
                block_dict['mining_tx'] = transaction_formatted

                block_dict['transactions'] = list(normal_transactions)

                blocks_dict[height] = dict(block_dict)
                i += 1

            old = height

        return blocks_dict

    def api_mempool(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns all the TX from mempool
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: list of mempool tx
        """
        # TEST V1/V2 ok, see test_mempool
        # txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)
        if mp.MEMPOOL is None:
            self.app_log.error(f"MEMPOOL is None")
            response_tuples = []
        else:
            # EGG_EVO: mempool still returns old style unstructured tuples with partial info atm.
            response_tuples = mp.MEMPOOL.transactions_to_send()
            # response_tuples = [transaction.to_tuple() for transaction in mempool_txs]
        connections.send(socket_handler, response_tuples)

    def api_getconfig(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns configuration
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: list of node configuration options
        """
        # TODO: Test V1/V2
        slots = tuple(self.config.__slots__)
        connections.send(socket_handler, {key: self.config.__getattribute__(key) for key in slots})

    def api_clearmempool(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Empty the current mempool
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: 'ok'
        """
        # TODO: Test V1/V2 to add to test_mempool
        mp.MEMPOOL.clear()
        connections.send(socket_handler, 'ok')

    def api_ping(self, socket_handler, db_handler: "DbHandler", peers) -> None:
        """
        Void, just to allow the client to keep the socket open (avoids timeout)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: 'api_pong'
        """
        connections.send(socket_handler, 'api_pong')

    def api_getaddressinfo(self, socket_handler, db_handler: "DbHandler", peers) -> None:
        """
        Returns a dict with
        known: Did that address appear on a transaction?
        pubkey: The DECODED pubkey of the address if it signed a transaction,
        """
        # TODO: To be tested, with all 3 addresses type
        info = {'known': False, 'pubkey': ''}
        # get the address
        address = connections.receive(socket_handler)
        # print('api_getaddressinfo', address)
        try:
            # format check
            if not SignerFactory.address_is_valid(address):
                self.app_log.info("Bad address format <{}>".format(address))
                connections.send(socket_handler, info)
                return
            try:
                info['known'] = db_handler.known_address(address)
                info['pubkey'] = db_handler.pubkeyget(address)
                # kept for legacy compatibility -
                # EGG: could need a switch whether it's a new address (not double encoded)
                # or a legacy one (double encoded)
            except Exception as e:
                self.app_log.warning("api_getaddressinfo: {}".format(e))

            connections.send(socket_handler, info)
        except Exception as e:
            self.app_log.warning(e)

    def api_getblockfromhash(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns a specific block based on the provided hash.
        Warning: format is strange: we provide a hash, so there should be at most one result.
        But we send back a dict, with height as key, and block (including height again) as value.
        Should be enough to only send the block.
        **BUT** do not change, this would break current implementations using the current format
        (json rpc server for instance).
        # TODO: To be added to test suite.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """

        block_hash = connections.receive(socket_handler)  # hex string expected
        block = db_handler.get_block_from_hash(block_hash)
        blocks = block.to_blocks_dict()
        connections.send(socket_handler, blocks)

    def api_getblockfromhashextra(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns a specific block based on the provided hash.
        similar to api_getblockfromhash, but sends block dict, not a dict of a dict.
        Also embeds last and next block hash, as well as block difficulty
        Needed for json-rpc server and btc like data.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: To be added to test suite.
        try:
            block_hash = connections.receive(socket_handler)
            block_object = db_handler.get_block_from_hash(block_hash)
            blocks = block_object.to_blocks_dict()
            block = list(blocks.values())[0]
            block_height = block['block_height']

            block["previous_block_hash"] = db_handler.get_block_hash_for_height(block_height - 1)
            block["next_block_hash"] = db_handler.get_block_hash_for_height(block_height + 1)
            block["difficulty"] = int(db_handler.get_difficulty_for_height(block_height))
            # This was not db format dependent,
            # but for consistency sake and allow for other dbs to be use in the future, processed as well.
            # int(float(db_handler.fetchone(db_handler.h, "SELECT difficulty FROM misc WHERE block_height = ?",
            # (block['block_height'],))))
            # print(block)
            connections.send(socket_handler, block)
        except Exception as e:
            self.app_log.warning("api_getblockfromhashextra {}".format(e))
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            raise

    def api_getblockfromheight(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns a specific block based on the provided height.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: To be added to test suit and make sure it's V1/V2 compatible
        height = connections.receive(socket_handler)
        block = db_handler.get_block(height)
        blocks = block.to_blocks_dict()
        connections.send(socket_handler, blocks)

    def api_getaddressrange(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns a given number of transactions, maximum of 500 entries.
        Ignores blocks where no transactions of a given address happened.
        Reorganizes parameters to a quickly accessible json.
        Unnecessary data are removed.

        :param socket_handler:
        :param db_handler: (UNUSED)
        :param peers: (UNUSED)
        :return:
        """
        # TODO: To be added to test suit and make sure it's V1/V2 compatible
        address = connections.receive(socket_handler)
        starting_block = connections.receive(socket_handler)
        limit = connections.receive(socket_handler)

        if limit > 500:
            limit = 500

        transactions = db_handler.get_address_range(address, starting_block, limit)
        blocks = transactions.to_blocks_dict()
        connections.send(socket_handler, blocks)

    def api_getblockrange(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns full blocks and transactions from a block range, maximum of 50 entries.
        Includes function format_raw_txs_diffs for formatting. Useful for big data / nosql storage.
        :param socket_handler:
        :param db_handler: (UNUSED)
        :param peers: (UNUSED)
        :return:
        """
        # TODO: TEST V1/V2

        start_block = connections.receive(socket_handler)
        limit = connections.receive(socket_handler)

        if limit > 50:
            limit = 50

        try:
            db_handler._execute_param(db_handler.h,
                                      'SELECT * FROM transactions WHERE block_height >= ? AND block_height < ?',
                                      (start_block, start_block+limit, ))
            raw_txs = db_handler.h.fetchall()

            db_handler._execute_param(db_handler.h,
                                      'SELECT difficulty FROM misc WHERE block_height >= ? AND block_height < ?',
                                      (start_block, start_block+limit, ))
            raw_diffs = db_handler.h.fetchall()

            reply = json.dumps(self.blocktojsondiffs(raw_txs, raw_diffs))

        except Exception as e:
            self.app_log.warning(e)
            raise

        connections.send(socket_handler, reply)

    def api_getblocksince(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the full blocks and transactions following a given block_height
        Returns at most transactions from 10 blocks (the most recent ones if it truncates)
        Used by the json-rpc server to poll and be notified of tx and new blocks.

        Returns full blocks and transactions following a given block_height.
        Given block_height should not be lower than the last 10 blocks.
        If given block_height is lower than the most recent block -10,
        last 10 blocks will be returned.

        **Used by the json-rpc server to poll and be notified of tx and new blocks** DO NOT REMOVE!!!.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: To be added to test suit and make sure it's V1/V2 compatible
        info = []
        # get the last known block
        since_height = connections.receive(socket_handler)
        # print('api_getblocksince', since_height)
        try:
            try:
                # what is the min block height to consider ?
                block_height = max(db_handler.block_height_max()-11, since_height)
                db_handler._execute_param(db_handler.h,
                                          'SELECT * FROM transactions WHERE block_height > ?',
                                          (block_height, ))
                info = db_handler.h.fetchall()
                # it's a list of tuples, send as is.
                # But if we are v2 db, conversion is needed to send back as legacy
                if not self.config.legacy_db:
                    info = [Transaction.from_v2(tx).to_tuple() for tx in info]
                # print(all)
            except Exception as e:
                print(e)
                raise
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            print(e)
            raise

    def api_getblockswhereoflike(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the full transactions following a given block_height and with openfield begining by the given string
        Returns at most transactions from 1440 blocks at a time (the most *older* ones if it truncates)
        so about 1 day worth of data.
        Maybe huge, use with caution and on restrictive queries only.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: To be added to test suit and make sure it's V1/V2 compatible
        info = []
        # get the last known block
        since_height = int(connections.receive(socket_handler))
        where_openfield_like = connections.receive(socket_handler) + '%'
        # print('api_getblockswhereoflike', since_height, where_openfield_like)
        try:
            try:
                # what is the max block height to consider ?
                block_height = min(db_handler.block_height_max(), since_height+1440)
                # print("block_height", since_height, block_height)
                db_handler._execute_param(db_handler.h, "SELECT * FROM transactions "
                                                        "WHERE block_height > ? and block_height <= ? "
                                                        "and openfield like ?",
                                          (since_height, block_height, where_openfield_like))
                info = db_handler.h.fetchall()
                # it's a list of tuples, send as is.
                # But if we are v2 db, conversion is needed to send back as legacy
                if not self.config.legacy_db:
                    info = [Transaction.from_v2(tx).to_tuple() for tx in info]

                # print("info", info)
            except Exception as e:
                self.app_log.warning(e)
                raise
            # Add the last fetched block so the client will be able to fetch the next block
            info.append([block_height])
            connections.send(socket_handler, info)
        except Exception as e:
            self.app_log.warning(e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            raise

    def api_getblocksafterwhere(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the full transactions following a given block_height and with specific conditions
        Returns at most transactions from 720 blocks at a time (the most *older* ones if it truncates)
        so about 12 hours worth of data.
        Maybe huge, use with caution and restrictive queries only.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        info = []
        # get the last known block
        since_height = connections.receive(socket_handler)
        where_conditions = connections.receive(socket_handler)
        self.app_log.warning('api_getblocksafterwhere', since_height, where_conditions)
        # TODO: feed as array to have a real control and avoid sql injection !important
        # Do *NOT* use in production until it's done.
        raise ValueError("Unsafe, do not use yet")
        """
        [
        ['','openfield','like','egg%']
        ]

        [
        ['', '('],
        ['','reward','>','0']
        ['and','recipient','in',['','','']]
        ['', ')'],
        ]
        """
        where_assembled = where_conditions
        conditions_assembled = ()
        try:
            try:
                # what is the max block height to consider ?
                block_height = min(db_handler.block_height_max(), since_height+720)
                # print("block_height",block_height)
                db_handler._execute_param(db_handler.h,
                                          "SELECT * FROM transactions "
                                          "WHERE block_height > ? and block_height <= ? and ( " + where_assembled + ")",
                                          (since_height, block_height) + conditions_assembled)
                info = db_handler.h.fetchall()
                # it's a list of tuples, send as is.
                # But if we are v2 db, conversion is needed to send back as legacy
                if not self.config.legacy_db:
                    info = [Transaction.from_v2(tx).to_tuple() for tx in info]

                # print(all)
            except Exception as e:
                self.app_log.warning(e)
                raise
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_getaddresssince(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the full transactions following a given block_height (will not include the given height)
        for the given address, with at least min_confirmations confirmations,
        as well as last considered block.
        Returns at most transactions from 720 blocks at a time (the most *older* ones if it truncates)
        so about 12 hours worth of data.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: To be added to test suit and make sure it's V1/V2 compatible
        info = []
        # get the last known block
        since_height = int(connections.receive(socket_handler))
        min_confirmations = int(connections.receive(socket_handler))
        address = str(connections.receive(socket_handler))
        print('api_getaddresssince', since_height, min_confirmations, address)
        try:
            try:
                # what is the max block height to consider ?
                block_height = min(db_handler.block_height_max() - min_confirmations, since_height+720)
                db_handler._execute_param(db_handler.h,
                                          'SELECT * FROM transactions WHERE block_height > ? AND block_height <= ? '
                                          'AND ((address = ?) OR (recipient = ?)) ORDER BY block_height ASC',
                                          (since_height, block_height, address, address))
                info = db_handler.h.fetchall()
                # But if we are v2 db, conversion is needed to send back as legacy
                if not self.config.legacy_db:
                    info = [Transaction.from_v2(tx).to_tuple() for tx in info]

            except Exception as e:
                print("Exception api_getaddresssince:".format(e))
                raise
            connections.send(socket_handler, {'last': block_height, 'minconf': min_confirmations, 'transactions': info})
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def _get_balance(self, db_handler: "DbHandler", address: str, minconf=1):
        """
        Queries the db to get the balance of a single address
        :param address:
        :param minconf:
        :return: balance as float (v1 DB) or integer (v2 DB)
        """
        try:
            # what is the max block height to consider ?
            max_block_height = db_handler.block_height_max() - minconf
            # calc balance up to this block_height
            db_handler._execute_param(db_handler.h,
                                      "SELECT sum(amount)+sum(reward) FROM transactions "
                                      "WHERE recipient = ? and block_height <= ?",
                                      (address, max_block_height))
            credit = db_handler.h.fetchone()[0]
            if not credit:
                credit = 0
            # debits + fee - reward
            db_handler._execute_param(db_handler.h,
                                      "SELECT sum(amount)+sum(fee) FROM transactions "
                                      "WHERE address = ? and block_height <= ?;",
                                      (address, max_block_height))
            debit = db_handler.h.fetchone()[0]
            if not debit:
                debit = 0
            # keep as float - Result will not be exact for v1 db
            # balance = '{:.8f}'.format(credit - debit)
            balance = credit - debit
        except Exception as e:
            # self.app_log.warning(e)
            raise
        return balance

    def api_getbalance(self, socket_handler, db_handler: "DbHandler", peers):
        """
        returns total balance for a list of addresses and minconf
        BEWARE: this is NOT the json rpc getbalance (that get balance for an account, not an address)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TEST V1/V2 ok see test_apihandler.py
        balance = 0
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                balance += self._get_balance(db_handler, address, minconf)
            # print('api_getbalance', addresses, minconf,':', balance)
            if not self.config.legacy_db:
                balance = balance / 100000000
            connections.send(socket_handler, balance)
        except Exception as e:
            raise

    def _get_received(self, db_handler: "DbHandler", address: str, minconf: int=1):
        """
        Queries the db to get the total received amount of a single address
        :param address:
        :param minconf:
        :return: balance as float (v1 DB) or integer (v2 DB)
        """
        try:
            # TODO : for this one and _get_balance, request max block height out of the loop
            # and pass it as a param to alleviate db load
            # what is the max block height to consider ?
            max_block_height = db_handler.block_height_max() - minconf
            # calc received up to this block_height
            db_handler._execute_param(db_handler.h,
                                      "SELECT sum(amount) FROM transactions "
                                      "WHERE recipient = ? and block_height <= ?;",
                                      (address, max_block_height))
            credit = db_handler.h.fetchone()[0]
            if not credit:
                credit = 0
        except Exception as e:
            # self.app_log.warning(e)
            raise
        return credit

    def api_getreceived(self, socket_handler, db_handler: "DbHandler", peers):
        """
        returns total received amount for a *list* of addresses and minconf
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TEST V1/V2 ok see test_apihandler.py
        received = 0
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                received += self._get_received(db_handler, address, minconf)
            if not self.config.legacy_db:
                received = received / 100000000
            print('api_getreceived', addresses, minconf, ':', received)
            connections.send(socket_handler, received)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_listreceived(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the total amount received for each given address with minconf, including empty addresses or not.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TEST V1/V2 ok see test_apihandler.py
        received = {}
        # TODO: this is temporary.
        # Will need more work to send full featured info needed for
        # https://bitcoin.org/en/developer-reference#listreceivedbyaddress
        # (confirmations and tx list)
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            include_empty = connections.receive(socket_handler)
            for address in addresses:
                temp = self._get_received(db_handler, address, minconf)
                if include_empty or temp > 0:
                    if not self.config.legacy_db:
                        temp = temp / 100000000
                    received[address] = temp
            print('api_listreceived', addresses, minconf, ':', received)
            connections.send(socket_handler, received)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_listbalance(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the total amount received for each given address with minconf, including empty addresses or not.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TEST V1/V2 ok see test_apihandler.py
        balances = {}
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            include_empty = connections.receive(socket_handler)
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                temp = self._get_balance(db_handler, address, minconf)
                if not self.config.legacy_db:
                    temp = temp / 100000000
                if include_empty or temp > 0:
                    balances[address] = temp
            print('api_listbalance', addresses, minconf, ':', balances)
            connections.send(socket_handler, balances)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_gettransaction(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the full transaction matching a tx id. Takes txid and format as params (json output if format is True)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: EGG: This one is to completely rewrite, too many low level code that should be handled by Transaction object
        raise ValueError("Do not use yet - Rewrite")
        transaction = {}
        try:
            # get the txid
            transaction_id = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            if self.config.old_sqlite:
                db_handler._execute_param(db_handler.h,
                                          "SELECT * FROM transactions WHERE signature like ?1",
                                          (transaction_id + '%', ))
            else:
                db_handler._execute_param(db_handler.h,
                                          "SELECT * FROM transactions "
                                          "WHERE substr(signature,1,4)=substr(?1,1,4) and  signature like ?1",
                                          (transaction_id+'%', ))
            raw = db_handler.h.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransaction', format, raw)
                return

            # current block height, needed for confirmations #
            block_height = db_handler.block_height_max()
            transaction['txid'] = transaction_id
            transaction['time'] = raw[1]
            transaction['hash'] = raw[5]
            transaction['address'] = raw[2]
            transaction['recipient'] = raw[3]
            transaction['amount'] = raw[4]
            transaction['fee'] = raw[8]
            transaction['reward'] = raw[9]
            transaction['operation'] = raw[10]
            transaction['openfield'] = raw[11]
            try:
                transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
            except Exception:
                transaction['pubkey'] = raw[6]  # support new pubkey schemes
            transaction['blockhash'] = raw[7]
            transaction['blockheight'] = raw[0]
            transaction['confirmations'] = block_height - raw[0]
            # Get more info on the block the tx is in.
            db_handler._execute_param(db_handler.h,
                                      "SELECT timestamp, recipient FROM transactions "
                                      "WHERE block_height= ? AND reward > 0",
                                      (raw[0], ))
            block_data = db_handler.h.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
            print('api_gettransaction', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_gettransactionbysignature(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns the full transaction matching a signature.
        Takes signature and format as params (json output if format is True)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        # TODO: EGG: This one is to completely rewrite, too many low level code that should be handled by Transaction object
        raise ValueError("Do not use yet - Rewrite")
        transaction = {}
        try:
            # get the txid
            signature = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            if self.config.old_sqlite:
                db_handler._execute_param(db_handler.h,
                                          "SELECT * FROM transactions WHERE signature = ?1",
                                          (signature, ))
            else:
                db_handler._execute_param(db_handler.h,
                                          "SELECT * FROM transactions "
                                          "WHERE substr(signature,1,4)=substr(?1,1,4) and  signature = ?1",
                                          (signature, ))
            raw = db_handler.h.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransactionbysignature', format, raw)
                return

            # current block height, needed for confirmations
            block_height = db_handler.block_height_max()
            transaction['signature'] = signature
            transaction['time'] = raw[1]
            transaction['hash'] = raw[5]
            transaction['address'] = raw[2]
            transaction['recipient'] = raw[3]
            transaction['amount'] = raw[4]
            transaction['fee'] = raw[8]
            transaction['reward'] = raw[9]
            transaction['operation'] = raw[10]
            transaction['openfield'] = raw[11]
            try:
                transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
            except Exception:
                transaction['pubkey'] = raw[6]  # support new pubkey schemes
            transaction['blockhash'] = raw[7]
            transaction['blockheight'] = raw[0]
            transaction['confirmations'] = block_height - raw[0]
            # Get more info on the block the tx is in.
            db_handler._execute_param(db_handler.h,
                                      "SELECT timestamp, recipient FROM transactions "
                                      "WHERE block_height= ? AND reward > 0",
                                      (raw[0], ))
            block_data = db_handler.h.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
            print('api_gettransactionbysignature', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_getpeerinfo(self, socket_handler, db_handler: "DbHandler", peers):
        """
        Returns a list of connected peers
        See https://bitcoin.org/en/developer-reference#getpeerinfo
        To be adjusted
        :return: list(dict)
        """
        # Do tests make sense in regnet config, with no peer?
        print('api_getpeerinfo')
        # TODO: Get what we can from peers, more will come when connections and connection stats will be modular, too.
        try:
            info = [{'id': id, 'addr': ip, 'inbound': True} for id, ip in enumerate(peers.consensus)]
            # TODO: peers will keep track of extra info, like port, last time, block_height aso.
            # TODO: add outbound connection
            connections.send(socket_handler, info)
        except Exception as e:
            self.app_log.warning(e)

    def api_gettransaction_for_recipients(self, socket_handler, db_handler: "DbHandler", peers):
            """
            Warning: this is currently very slow
            Returns the full transaction matching a tx id for a list of recipient addresses.
            Takes txid and format as params (json output if format is True)
            :param socket_handler:
            :param db_handler:
            :param peers:
            :return:
            """
            # TODO: EGG: This one is to completely rewrite, too many low level code that should be handled by Transaction object
            raise ValueError("Do not use yet - Rewrite")
            transaction = {}
            try:
                # get the txid
                transaction_id = connections.receive(socket_handler)
                # then the recipient list
                addresses = connections.receive(socket_handler)
                # and format
                format = connections.receive(socket_handler)
                recipients = json.dumps(addresses).replace("[", "(").replace(']', ')')  # format as sql
                # raw tx details
                if self.config.old_sqlite:
                    db_handler._execute_param(db_handler.h,
                                              "SELECT * FROM transactions WHERE recipient IN {} AND signature LIKE ?1"
                                              .format(recipients),
                                              (transaction_id + '%', ))
                else:
                    db_handler._execute_param(db_handler.h,
                                              "SELECT * FROM transactions "
                                              "WHERE recipient IN {} AND substr(signature,1,4)=substr(?1,1,4) "
                                              "and signature LIKE ?1".format(recipients),
                                              (transaction_id + '%', ))

                raw = db_handler.h.fetchone()
                if not format:
                    connections.send(socket_handler, raw)
                    print('api_gettransaction_for_recipients', format, raw)
                    return

                # current block height, needed for confirmations #
                block_height = db_handler.block_height_max()

                transaction['txid'] = transaction_id
                transaction['time'] = raw[1]
                transaction['hash'] = raw[5]
                transaction['address'] = raw[2]
                transaction['recipient'] = raw[3]
                transaction['amount'] = raw[4]
                transaction['fee'] = raw[8]
                transaction['reward'] = raw[9]
                transaction['operation'] = raw[10]
                transaction['openfield'] = raw[11]

                try:
                    transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
                except Exception:
                    transaction['pubkey'] = raw[6]  # support new pubkey schemes

                transaction['blockhash'] = raw[7]
                transaction['blockheight'] = raw[0]
                transaction['confirmations'] = block_height - raw[0]
                # Get more info on the block the tx is in.
                db_handler._execute_param(db_handler.h,
                                          "SELECT timestamp, recipient FROM transactions "
                                          "WHERE block_height= ? AND reward > 0",
                                          (raw[0], ))
                block_data = db_handler.h.fetchone()
                transaction['blocktime'] = block_data[0]
                transaction['blockminer'] = block_data[1]
                print('api_gettransaction_for_recipients', format, transaction)
                connections.send(socket_handler, transaction)
            except Exception as e:
                # self.app_log.warning(e)
                raise
