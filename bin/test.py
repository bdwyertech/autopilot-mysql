import json
from Queue import Queue as Queue
from Queue import Empty, Full
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

import mock
import manage
from manage.containerpilot import ContainerPilot
from manage.libconsul import Consul
from manage.libmanta import Manta
from manage.libmysql import MySQL
from manage.utils import *


thread_data = threading.local()

def trace_exceptions(frame, event, arg):
    if event != 'exception':
        return
    co = frame.f_code
    func_name = co.co_name
    line_no = frame.f_lineno
    exc_type, exc_value, exc_traceback = arg
    # print 'Tracing exception: %s "%s" on line %s of %s' % \
    #   (exc_type.__name__, exc_value, line_no, func_name)

def trace_wait(frame, event, arg):
    if event != 'call':
        return
    func_name = frame.f_code.co_name
    if func_name in thread_data.trace_into:
        node_name = threading.current_thread().name
        # print('{} is waiting for {}'.format(node_name, func_name))
        try:
            # block until new work is available but no more than 5
            # seconds before killing the thread
            val = thread_data.queue.get(True, 5)
            thread_data.queue.task_done()
        except Empty:
            sys.exit(0)
        # print('{} finished waiting for {}: {}'.format(node_name, func_name, val))
        return trace_exceptions


def run_trace(fn, *args, **kwargs):
    try:
        thread_data.queue = kwargs.pop('queue')
        thread_data.trace_into = kwargs.pop('trace_into', None)
        sys.settrace(trace_wait)
        fn(*args, **kwargs)
    except Exception as ex:
        print(ex)


def example(arg):

    print('example: {}'.format(arg))

    def example_one(arg):
        print('example_one: {}'.format(arg))

    def example_two(arg):
        print('example_two: {}'.format(arg))

    def example_three(arg):
        print('example_three: {}'.format(arg))

    example_one(arg)
    example_two(arg)
    example_three(arg)

    print('exiting example: {}'.format(arg))

class TestPreStart(unittest.TestCase):

    def setUp(self):
        self.node1 = manage.Node()
        self.node1.name = 'node1'
        self.node2 = manage.Node()
        self.node2.name = 'node2'
        self.node1q = Queue(1)
        self.node2q = Queue(1)

    def test_ordering(self):

        trace_into = ['example', 'example_one', 'example_two', 'example_three']
        node1 = threading.Thread(target=run_trace,
                                 name='node1',
                                 args=(example, 'node1'),
                                 kwargs=dict(queue=self.node1q,
                                             trace_into=trace_into))
        node2 = threading.Thread(target=run_trace,
                                 name='node2',
                                 args=(example, 'node2'),
                                 kwargs=dict(queue=self.node2q,
                                             trace_into=trace_into))
        print('starting!')
        node1.start()
        node2.start()

        try:
            while True:
                # tick thru the functions
                print('tick!')
                self.node1q.put(1, True, 1)
                print('tick!')
                self.node2q.put(2, True, 1)
                time.sleep(1)
        except Full:
            pass
        print('waiting!')
        node1.join()
        node2.join()


    # def test_pre_start_ordering(self):
    #     """
    #     We can't easily interleave the steps of two different nodes
    #     pre_start in a predictable way
    #     """
    #     manage.pre_start(self.node1)
    #     manage.pre_start(self.node2)


class TestMySQL(unittest.TestCase):

    def setUp(self):
        logging.getLogger('manage').setLevel(logging.WARN)
        self.environ = {
            'MYSQL_DATABASE': 'test_mydb',
            'MYSQL_USER': 'test_me',
            'MYSQL_PASSWORD': 'test_pass',
            'MYSQL_ROOT_PASSWORD': 'test_root_pass',
            'MYSQL_RANDOM_ROOT_PASSWORD': 'Y',
            'MYSQL_ONETIME_PASSWORD': '1',
            'MYSQL_REPL_USER': 'test_repl_user',
            'MYSQL_REPL_PASSWORD': 'test_repl_pass',
            'INNODB_BUFFER_POOL_SIZE': '100'
        }
        self.my = MySQL(self.environ)
        self.my._conn = mock.MagicMock()

    def tearDown(self):
        logging.getLogger('manage').setLevel(logging.DEBUG)

    def test_parse(self):
        self.assertEqual(self.my.mysql_db, 'test_mydb')
        self.assertEqual(self.my.mysql_user, 'test_me')
        self.assertEqual(self.my.mysql_password, 'test_pass')
        self.assertEqual(self.my.mysql_root_password, 'test_root_pass')
        self.assertEqual(self.my.mysql_random_root_password, True)
        self.assertEqual(self.my.mysql_onetime_password, True)
        self.assertEqual(self.my.repl_user, 'test_repl_user')
        self.assertEqual(self.my.repl_password, 'test_repl_pass')
        self.assertEqual(self.my.datadir, '/var/lib/mysql')
        self.assertEqual(self.my.pool_size, 100)
        self.assertIsNotNone(self.my.ip)

    def test_query_buffer_execute_should_flush(self):
        self.my.add('query 1', ())
        self.assertEqual(len(self.my._query_buffer.items()), 1)
        self.assertEqual(len(self.my._conn.mock_calls), 0)
        self.my.execute('query 2', ())
        self.assertEqual(len(self.my._query_buffer.items()), 0)
        exec_calls = [
            mock.call.cursor().execute('query 1', params=()),
            mock.call.cursor().execute('query 2', params=()),
            mock.call.commit(),
            mock.call.cursor().close()
        ]
        self.assertEqual(self.my._conn.mock_calls[2:], exec_calls)

    def test_query_buffer_execute_many_should_flush(self):
        self.my.add('query 3', ())
        self.my.add('query 4', ())
        self.my.add('query 5', ())
        self.my.execute_many()
        self.assertEqual(len(self.my._query_buffer.items()), 0)
        exec_many_calls = [
            mock.call.cursor().execute('query 3', params=()),
            mock.call.cursor().execute('query 4', params=()),
            mock.call.cursor().execute('query 5', params=()),
            mock.call.commit(),
            mock.call.cursor().close()
        ]
        self.assertEqual(self.my._conn.mock_calls[2:], exec_many_calls)

    def test_query_buffer_query_should_flush(self):
        self.my.query('query 6', ())
        self.assertEqual(len(self.my._query_buffer.items()), 0)
        query_calls = [
            mock.call.cursor().execute('query 6', params=()),
            mock.call.cursor().fetchall(),
            mock.call.cursor().close()
        ]
        self.assertEqual(self.my._conn.mock_calls[2:], query_calls)

    def test_expected_setup_statements(self):
        conn = mock.MagicMock()
        self.my.setup_root_user(conn)
        self.my.create_db(conn)
        self.my.create_default_user(conn)
        self.my.create_repl_user(conn)
        self.my.expire_root_password(conn)
        self.assertEqual(len(self.my._conn.mock_calls), 0) # use param, not attr
        statements = [args[0] for (name, args, _)
                      in conn.mock_calls if name == 'cursor().execute']
        expected = [
            'SET @@SESSION.SQL_LOG_BIN=0;',
            "DELETE FROM `mysql`.`user` where user != 'mysql.sys';",
            'CREATE USER `root`@`%` IDENTIFIED BY %s ;',
            'GRANT ALL ON *.* TO `root`@`%` WITH GRANT OPTION ;',
            'DROP DATABASE IF EXISTS test ;',
            'FLUSH PRIVILEGES ;',
            'CREATE DATABASE IF NOT EXISTS `test_mydb`;',
            'CREATE USER `test_me`@`%` IDENTIFIED BY %s;',
            'GRANT ALL ON `test_mydb`.* TO `test_me`@`%`;',
            'FLUSH PRIVILEGES;',
            'CREATE USER `test_repl_user`@`%` IDENTIFIED BY %s; ',
            ('GRANT SUPER, SELECT, INSERT, REPLICATION SLAVE, RELOAD,'
             ' LOCK TABLES, GRANT OPTION, REPLICATION CLIENT, RELOAD,'
             ' DROP, CREATE ON *.* TO `test_repl_user`@`%`; '),
            'FLUSH PRIVILEGES;',
            'ALTER USER `root`@`%` PASSWORD EXPIRE']
        self.assertEqual(statements, expected)


class TestConsul(unittest.TestCase):

    def setUp(self):
        self.environ = {
            'CONSUL': 'my.consul.example.com',
            'CONSUL_AGENT': '1',
        }

    def test_parse_with_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '1'
        consul = Consul(self.environ)
        self.assertEqual(consul.host, 'localhost')

    def test_parse_without_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '0'
        consul = Consul(self.environ)
        self.assertEqual(consul.host, 'my.consul.example.com')

        self.environ['CONSUL_AGENT'] = ''
        consul = Consul(self.environ)
        self.assertEqual(consul.host, 'my.consul.example.com')


class TestContainerPilotConfig(unittest.TestCase):

    def setUp(self):
        logging.getLogger('manage').setLevel(logging.WARN)
        self.environ = {
            'CONSUL': 'my.consul.example.com',
            'CONSUL_AGENT': '1',
            'CONTAINERPILOT': 'file:///etc/containerpilot.json'
        }

    def tearDown(self):
        logging.getLogger('manage').setLevel(logging.DEBUG)

    def test_parse_with_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'localhost:8500')
        cmd = cp.config['coprocesses'][0]['command']
        host_cfg_idx = cmd.index('-retry-join') + 1
        self.assertEqual(cmd[host_cfg_idx], 'my.consul.example.com:8500')
        self.assertEqual(cp.state, REPLICA)

    def test_parse_without_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '0'
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertEqual(cp.config['coprocesses'], [])
        self.assertEqual(cp.state, REPLICA)

        self.environ['CONSUL_AGENT'] = ''
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertEqual(cp.config['coprocesses'], [])
        self.assertEqual(cp.state, REPLICA)

    def test_update(self):
        self.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        temp_file = tempfile.NamedTemporaryFile()
        cp.path = temp_file.name

        # no update expected
        cp.update()
        with open(temp_file.name, 'r') as updated:
            self.assertEqual(updated.read(), '')

        # force an update
        cp.state = UNASSIGNED
        cp.update()
        with open(temp_file.name, 'r') as updated:
            config = json.loads(updated.read())
            self.assertEqual(config['consul'], 'localhost:8500')
            cmd = config['coprocesses'][0]['command']
            host_cfg_idx = cmd.index('-retry-join') + 1
            self.assertEqual(cmd[host_cfg_idx], 'my.consul.example.com:8500')


class TestMantaConfig(unittest.TestCase):

    def setUp(self):
        self.environ = {
            'MANTA_USER': 'test_manta_account',
            'MANTA_SUBUSER': 'test_manta_subuser',
            'MANTA_ROLE': 'test_manta_role',
            'MANTA_KEY_ID': '49:d5:1f:09:5e:46:92:14:c0:46:8e:48:33:75:10:bc',
            'MANTA_PRIVATE_KEY': (
                '-----BEGIN RSA PRIVATE KEY-----#'
                'MIIEowIBAAKCAQEAvvljJQt2V3jJoM1SC9FiaBaw5AjVR40v5wKCVaONSz+FWm#'
                'pc91hUJHQClaxXDlf1p5kf3Oqu5qjM6w8oD7uPkzj++qPnCkzt+JGPfUBxpzul#'
                '80J0GLHpqQ2YUBXfJ6pCb0g7z/hkdsSwJt7DS+keWCtWpVYswj2Ln8CwNlZlye#'
                'qAmNE2ePZg8AzfpFmDROljU3GHhKaAviiLyxOklbwSbySbTmdNLHHxu22+ciW9#'
                '-----END RSA PRIVATE KEY-----')
        }

    def test_parse(self):
        manta = Manta(self.environ)
        self.assertEqual(manta.account, 'test_manta_account')
        self.assertEqual(manta.user, 'test_manta_subuser')
        self.assertEqual(manta.role, 'test_manta_role')
        self.assertEqual(manta.bucket, '/test_manta_account/stor')
        self.assertEqual(manta.url, 'https://us-east.manta.joyent.com')
        self.assertEqual(
            manta.private_key,
            ('-----BEGIN RSA PRIVATE KEY-----\n'
             'MIIEowIBAAKCAQEAvvljJQt2V3jJoM1SC9FiaBaw5AjVR40v5wKCVaONSz+FWm\n'
             'pc91hUJHQClaxXDlf1p5kf3Oqu5qjM6w8oD7uPkzj++qPnCkzt+JGPfUBxpzul\n'
             '80J0GLHpqQ2YUBXfJ6pCb0g7z/hkdsSwJt7DS+keWCtWpVYswj2Ln8CwNlZlye\n'
             'qAmNE2ePZg8AzfpFmDROljU3GHhKaAviiLyxOklbwSbySbTmdNLHHxu22+ciW9\n'
             '-----END RSA PRIVATE KEY-----'))
        self.assertEqual(manta.key_id,
                         '49:d5:1f:09:5e:46:92:14:c0:46:8e:48:33:75:10:bc')


class TestUtilsEnvironment(unittest.TestCase):

    def test_to_flag(self):
        self.assertEqual(to_flag('yes'), True)
        self.assertEqual(to_flag('Y'), True)
        self.assertEqual(to_flag('no'), False)
        self.assertEqual(to_flag('N'), False)
        self.assertEqual(to_flag('1'), True)
        self.assertEqual(to_flag('xxxxx'), True)
        self.assertEqual(to_flag('0'), False)
        self.assertEqual(to_flag('xxxxx'), True)
        self.assertEqual(to_flag(1), True)
        self.assertEqual(to_flag(0), False)

    def test_env_parse(self):

        os.environ['TestUtilsEnvironment'] = 'PASS'
        environ = {
            'A': '$TestUtilsEnvironment',
            'B': 'PASS  ',
            'C': 'PASS # SOME COMMENT'
        }
        self.assertEqual(env('A', '', environ), 'PASS')
        self.assertEqual(env('B', '', environ), 'PASS')
        self.assertEqual(env('C', '', environ), 'PASS')
        self.assertEqual(env('D', 'PASS', environ), 'PASS')



if __name__ == '__main__':
    unittest.main()
