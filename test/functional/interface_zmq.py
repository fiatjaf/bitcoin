#!/usr/bin/env python3
# Copyright (c) 2015-2019 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test the ZMQ notification interface."""
import struct

from test_framework.address import ADDRESS_BCRT1_UNSPENDABLE
from test_framework.test_framework import BitcoinTestFramework
from test_framework.messages import CTransaction, hash256
from test_framework.util import assert_equal, connect_nodes
from io import BytesIO
from time import sleep

def hash256_reversed(byte_str):
    return hash256(byte_str)[::-1]

class ZMQSubscriber:
    def __init__(self, socket, topic):
        self.sequence = 0
        self.socket = socket
        self.topic = topic

        import zmq
        self.socket.setsockopt(zmq.SUBSCRIBE, self.topic)

    def receive(self, specific_topic = None):
        expected_topic = specific_topic if specific_topic else self.topic

        topic, body, seq = self.socket.recv_multipart()
        # Topic should match the subscriber topic.
        assert_equal(topic, expected_topic)
        # Sequence should be incremental.
        assert_equal(struct.unpack('<I', seq)[-1], self.sequence)
        self.sequence += 1
        return body


class ZMQTest (BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 2

    def skip_test_if_missing_module(self):
        self.skip_if_no_py3_zmq()
        self.skip_if_no_bitcoind_zmq()

    def run_test(self):
        import zmq
        self.ctx = zmq.Context()
        try:
            self.test_basic()
            self.test_reorg()
            self.test_multiple_interfaces()
        finally:
            # Destroy the ZMQ context.
            self.log.debug("Destroying ZMQ context")
            self.ctx.destroy(linger=None)

    def test_basic(self):
        # All messages are received in the same socket which means
        # that this test fails if the publishing order changes.
        # Note that the publishing order is not defined in the documentation and
        # is subject to change.
        import zmq

        # Invalid zmq arguments don't take down the node, see #17185.
        self.restart_node(0, ["-zmqpubrawtx=foo", "-zmqpubhashtx=bar"])

        address = 'tcp://127.0.0.1:28332'
        socket = self.ctx.socket(zmq.SUB)
        socket.set(zmq.RCVTIMEO, 60000)

        # Subscribe to all available topics.
        hashblock = ZMQSubscriber(socket, b"hashblock")
        hashtx = ZMQSubscriber(socket, b"hashtx")
        rawblock = ZMQSubscriber(socket, b"rawblock")
        rawtx = ZMQSubscriber(socket, b"rawtx")
        if self.is_wallet_compiled():
            self.hashwallettx = ZMQSubscriber(socket, b"hashwallettx")
            self.rawwallettx = ZMQSubscriber(socket, b"rawwallettx")

        self.restart_node(0, ["-zmqpub%s=%s" % (sub.topic.decode(), address) for sub in [hashblock, hashtx, rawblock, rawtx, getattr(self, 'hashwallettx', None), getattr(self, 'rawwallettx', None)] if sub is not None])
        connect_nodes(self.nodes[0], 1)
        socket.connect(address)
        # Relax so that the subscriber is ready before publishing zmq messages
        sleep(0.2)

        if self.is_wallet_compiled():
            self.sync_all()
            # Flush initial wallettx events before we begin
            while True:
                try:
                    topic, body, seq = self.hashwallettx.socket.recv_multipart()
                except zmq.ZMQError:
                    break
                subscriber = {b'hashwallettx-block': self.hashwallettx, b'rawwallettx-block': self.rawwallettx}[topic]
                assert_equal(struct.unpack('<I', seq)[-1], subscriber.sequence)
                subscriber.sequence += 1

        num_blocks = 5
        self.log.info("Generate %(n)d blocks (and %(n)d coinbase txes)" % {"n": num_blocks})
        if self.is_wallet_compiled():
            genhashes = self.nodes[0].generate(num_blocks)
        else:
            genhashes = self.nodes[0].generatetoaddress(num_blocks, ADDRESS_BCRT1_UNSPENDABLE)

        self.sync_all()

        for x in range(num_blocks):
            # Should receive the coinbase txid.
            txid = hashtx.receive()

            # Should receive the coinbase raw transaction.
            hex = rawtx.receive()
            tx = CTransaction()
            tx.deserialize(BytesIO(hex))
            tx.calc_sha256()
            assert_equal(tx.hash, txid.hex())

            if self.is_wallet_compiled():
                # Should receive wallet tx
                wallettxid = self.hashwallettx.receive(b"hashwallettx-block")
                wallethex = self.rawwallettx.receive(b"rawwallettx-block")
                wallettx = CTransaction()
                wallettx.deserialize(BytesIO(wallethex))
                wallettx.calc_sha256()
                assert_equal(wallettx.hash, wallettxid.hex())

            # Should receive the generated block hash.
            hash = hashblock.receive().hex()
            assert_equal(genhashes[x], hash)
            # The block should only have the coinbase txid.
            assert_equal([txid.hex()], self.nodes[1].getblock(hash)["tx"])

            # Should receive the generated raw block.
            block = rawblock.receive()
            assert_equal(genhashes[x], hash256_reversed(block[:80]).hex())

        if self.is_wallet_compiled():
            self.log.info("Wait for tx from second node")
            payment_txid = self.nodes[1].sendtoaddress(self.nodes[0].getnewaddress(), 1.0)
            self.sync_all()

            # Should receive the broadcasted txid.
            txid = hashtx.receive()
            assert_equal(payment_txid, txid.hex())

            # Should receive the broadcasted raw transaction.
            hex = rawtx.receive()
            assert_equal(payment_txid, hash256_reversed(hex).hex())

        if self.is_wallet_compiled():
            wallettxid = self.hashwallettx.receive(b"hashwallettx-mempool")
            wallethex = self.rawwallettx.receive(b"rawwallettx-mempool")
            assert_equal(hash256_reversed(wallethex), wallettxid)

        self.log.info("Test the getzmqnotifications RPC")
        assert_equal(self.nodes[0].getzmqnotifications(), [
            {"type": "pubhashblock", "address": address, "hwm": 1000},
            {"type": "pubhashtx", "address": address, "hwm": 1000},
            ] + ([{"type": "pubhashwallettx", "address": address, "hwm": 1000}] if self.is_wallet_compiled() else []) + [
            {"type": "pubrawblock", "address": address, "hwm": 1000},
            {"type": "pubrawtx", "address": address, "hwm": 1000},
            ] + ([{"type": "pubrawwallettx", "address": address, "hwm": 1000}] if self.is_wallet_compiled() else []) + [
        ])

        assert_equal(self.nodes[1].getzmqnotifications(), [])

    def test_reorg(self):
        import zmq
        address = 'tcp://127.0.0.1:28333'
        socket = self.ctx.socket(zmq.SUB)
        socket.set(zmq.RCVTIMEO, 60000)
        hashblock = ZMQSubscriber(socket, b'hashblock')

        # Should only notify the tip if a reorg occurs
        self.restart_node(0, ['-zmqpub%s=%s' % (hashblock.topic.decode(), address)])
        socket.connect(address)
        # Relax so that the subscriber is ready before publishing zmq messages
        sleep(0.2)

        # Generate 1 block in nodes[0] and receive all notifications
        self.nodes[0].generatetoaddress(1, ADDRESS_BCRT1_UNSPENDABLE)
        assert_equal(self.nodes[0].getbestblockhash(), hashblock.receive().hex())

        # Generate 2 blocks in nodes[1]
        self.nodes[1].generatetoaddress(2, ADDRESS_BCRT1_UNSPENDABLE)

        # nodes[0] will reorg chain after connecting back nodes[1]
        connect_nodes(self.nodes[0], 1)

        # Should receive nodes[1] tip
        assert_equal(self.nodes[1].getbestblockhash(), hashblock.receive().hex())

    def test_multiple_interfaces(self):
        import zmq

        # Set up two subscribers with different addresses
        subscribers = []
        for x in range(2):
            address = 'tcp://127.0.0.1:%d' % (28334 + x)
            socket = self.ctx.socket(zmq.SUB)
            socket.set(zmq.RCVTIMEO, 60000)
            hashblock = ZMQSubscriber(socket, b"hashblock")
            socket.connect(address)
            subscribers.append({'address': address, 'socket': socket, 'hashblock': hashblock})

        self.restart_node(0, ['-zmqpub%s=%s' % (subscribers[0]['hashblock'].topic.decode(), subscribers[0]['address']),
            '-zmqpub%s=%s' % (subscribers[1]['hashblock'].topic.decode(), subscribers[1]['address'])])
        # Relax so that the subscriber is ready before publishing zmq messages
        sleep(0.2)

        # Generate 1 block in nodes[0] and receive all notifications
        self.nodes[0].generatetoaddress(1, ADDRESS_BCRT1_UNSPENDABLE)

        # Should receive the same block hash on both subscribers
        assert_equal(self.nodes[0].getbestblockhash(), subscribers[0]['hashblock'].receive().hex())
        assert_equal(self.nodes[0].getbestblockhash(), subscribers[1]['hashblock'].receive().hex())

if __name__ == '__main__':
    ZMQTest().main()
