# -*- coding: utf-8 -*-
import warnings
import pymysql.cursors
import pymongo
import redis
import aerospike
from tzlocal import get_localzone
import datetime
import os


class ExternalSource(object):
    def __init__(self, name, internal_hostname, internal_port,
                 docker_hostname, docker_port, user, password):
        self.name = name
        self.internal_hostname = internal_hostname
        self.internal_port = int(internal_port)
        self.docker_hostname = docker_hostname
        self.docker_port = int(docker_port)
        self.user = user
        self.password = password

    def get_source_str(self, table_name):
        raise NotImplementedError("Method {} is not implemented for {}".format(
            "get_source_config_part", self.__class__.__name__))

    def prepare(self, structure, table_name, cluster):
        raise NotImplementedError("Method {} is not implemented for {}".format(
            "prepare_remote_source", self.__class__.__name__))

    # data is banch of Row
    def load_data(self, data):
        raise NotImplementedError("Method {} is not implemented for {}".format(
            "prepare_remote_source", self.__class__.__name__))

    def compatible_with_layout(self, layout):
        return True


class SourceMySQL(ExternalSource):
    TYPE_MAPPING = {
        'UInt8': 'tinyint unsigned',
        'UInt16': 'smallint unsigned',
        'UInt32': 'int unsigned',
        'UInt64': 'bigint unsigned',
        'Int8': 'tinyint',
        'Int16': 'smallint',
        'Int32': 'int',
        'Int64': 'bigint',
        'UUID': 'varchar(36)',
        'Date': 'date',
        'DateTime': 'datetime',
        'String': 'text',
        'Float32': 'float',
        'Float64': 'double'
    }

    def create_mysql_conn(self):
        self.connection = pymysql.connect(
            user=self.user,
            password=self.password,
            host=self.internal_hostname,
            port=self.internal_port)

    def execute_mysql_query(self, query):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.connection.cursor() as cursor:
                cursor.execute(query)
            self.connection.commit()

    def get_source_str(self, table_name):
        return '''
                <mysql>
                    <replica>
                        <priority>1</priority>
                        <host>127.0.0.1</host>
                        <port>3333</port>   <!-- Wrong port, for testing basic failover to work. -->
                    </replica>
                    <replica>
                        <priority>2</priority>
                        <host>{hostname}</host>
                        <port>{port}</port>
                    </replica>
                    <user>{user}</user>
                    <password>{password}</password>
                    <db>test</db>
                    <table>{tbl}</table>
                </mysql>'''.format(
                hostname=self.docker_hostname,
                port=self.docker_port,
                user=self.user,
                password=self.password,
                tbl=table_name,
            )

    def prepare(self, structure, table_name, cluster):
        self.create_mysql_conn()
        self.execute_mysql_query("create database if not exists test default character set 'utf8'")
        fields_strs = []
        for field in structure.keys + structure.ordinary_fields + structure.range_fields:
            fields_strs.append(field.name + ' ' + self.TYPE_MAPPING[field.field_type])
        create_query = '''create table test.{table_name} (
            {fields_str});
        '''.format(table_name=table_name, fields_str=','.join(fields_strs))
        self.execute_mysql_query(create_query)
        self.ordered_names = structure.get_ordered_names()
        self.prepared = True

    def load_data(self, data, table_name):
        values_strs = []
        if not data:
            return
        for row in data:
            sorted_row = []
            for name in self.ordered_names:
                data = row.data[name]
                if isinstance(row.data[name], str):
                    data = "'" + data + "'"
                else:
                    data = str(data)
                sorted_row.append(data)
            values_strs.append('(' + ','.join(sorted_row) + ')')
        query = 'insert into test.{} ({}) values {}'.format(
            table_name,
            ','.join(self.ordered_names),
            ','.join(values_strs))
        self.execute_mysql_query(query)


class SourceMongo(ExternalSource):

    def get_source_str(self, table_name):
        return '''
            <mongodb>
                <host>{host}</host>
                <port>{port}</port>
                <user>{user}</user>
                <password>{password}</password>
                <db>test</db>
                <collection>{tbl}</collection>
            </mongodb>
        '''.format(
            host=self.docker_hostname,
            port=self.docker_port,
            user=self.user,
            password=self.password,
            tbl=table_name,
        )

    def prepare(self, structure, table_name, cluster):
        connection_str = 'mongodb://{user}:{password}@{host}:{port}'.format(
            host=self.internal_hostname, port=self.internal_port,
            user=self.user, password=self.password)
        self.connection = pymongo.MongoClient(connection_str)
        self.converters = {}
        for field in structure.get_all_fields():
            if field.field_type == "Date":
                self.converters[field.name] = lambda x: datetime.datetime.strptime(x, "%Y-%m-%d")
            elif field.field_type == "DateTime":
                self.converters[field.name] = lambda x: get_localzone().localize(datetime.datetime.strptime(x, "%Y-%m-%d %H:%M:%S"))
            else:
                self.converters[field.name] = lambda x: x

        self.db = self.connection['test']
        self.db.add_user(self.user, self.password)
        self.prepared = True

    def load_data(self, data, table_name):
        tbl = self.db[table_name]

        to_insert = []
        for row in data:
            row_dict = {}
            for cell_name, cell_value in row.data.items():
                row_dict[cell_name] = self.converters[cell_name](cell_value)
            to_insert.append(row_dict)

        result = tbl.insert_many(to_insert)

class SourceMongoURI(SourceMongo):
    def compatible_with_layout(self, layout):
        # It is enough to test one layout for this dictionary, since we're
        # only testing that the connection with URI works.
        return layout.name == 'flat'

    def get_source_str(self, table_name):
        return '''
            <mongodb>
                <uri>mongodb://{user}:{password}@{host}:{port}/test</uri>
                <collection>{tbl}</collection>
            </mongodb>
        '''.format(
            host=self.docker_hostname,
            port=self.docker_port,
            user=self.user,
            password=self.password,
            tbl=table_name,
        )

class SourceClickHouse(ExternalSource):

    def get_source_str(self, table_name):
        return '''
            <clickhouse>
                <host>{host}</host>
                <port>{port}</port>
                <user>{user}</user>
                <password>{password}</password>
                <db>test</db>
                <table>{tbl}</table>
            </clickhouse>
        '''.format(
            host=self.docker_hostname,
            port=self.docker_port,
            user=self.user,
            password=self.password,
            tbl=table_name,
        )

    def prepare(self, structure, table_name, cluster):
        self.node = cluster.instances[self.docker_hostname]
        self.node.query("CREATE DATABASE IF NOT EXISTS test")
        fields_strs = []
        for field in structure.keys + structure.ordinary_fields + structure.range_fields:
            fields_strs.append(field.name + ' ' + field.field_type)
        create_query = '''CREATE TABLE test.{table_name} (
            {fields_str}) ENGINE MergeTree ORDER BY tuple();
        '''.format(table_name=table_name, fields_str=','.join(fields_strs))
        self.node.query(create_query)
        self.ordered_names = structure.get_ordered_names()
        self.prepared = True

    def load_data(self, data, table_name):
        values_strs = []
        if not data:
            return
        for row in data:
            sorted_row = []
            for name in self.ordered_names:
                row_data = row.data[name]
                if isinstance(row_data, str):
                    row_data = "'" + row_data + "'"
                else:
                    row_data = str(row_data)
                sorted_row.append(row_data)
            values_strs.append('(' + ','.join(sorted_row) + ')')
        query = 'INSERT INTO test.{} ({}) values {}'.format(
            table_name,
            ','.join(self.ordered_names),
            ','.join(values_strs))
        self.node.query(query)


class SourceFile(ExternalSource):

    def get_source_str(self, table_name):
        table_path = "/" + table_name + ".tsv"
        return '''
            <file>
                <path>{path}</path>
                <format>TabSeparated</format>
            </file>
        '''.format(
            path=table_path,
        )

    def prepare(self, structure, table_name, cluster):
        self.node = cluster.instances[self.docker_hostname]
        path = "/" + table_name + ".tsv"
        self.node.exec_in_container(["bash", "-c", "touch {}".format(path)], user="root")
        self.ordered_names = structure.get_ordered_names()
        self.prepared = True

    def load_data(self, data, table_name):
        if not data:
            return
        path = "/" + table_name + ".tsv"
        for row in list(data):
            sorted_row = []
            for name in self.ordered_names:
                sorted_row.append(str(row.data[name]))

            str_data = '\t'.join(sorted_row)
            self.node.exec_in_container(["bash", "-c", "echo \"{row}\" >> {fname}".format(row=str_data, fname=path)], user="root")

    def compatible_with_layout(self, layout):
        return 'cache' not in layout.name and 'direct' not in layout.name


class _SourceExecutableBase(ExternalSource):

    def _get_cmd(self, path):
        raise NotImplementedError("Method {} is not implemented for {}".format(
            "_get_cmd", self.__class__.__name__))

    def get_source_str(self, table_name):
        table_path = "/" + table_name + ".tsv"
        return '''
            <executable>
                <command>{cmd}</command>
                <format>TabSeparated</format>
            </executable>
        '''.format(
            cmd=self._get_cmd(table_path),
        )

    def prepare(self, structure, table_name, cluster):
        self.node = cluster.instances[self.docker_hostname]
        path = "/" + table_name + ".tsv"
        self.node.exec_in_container(["bash", "-c", "touch {}".format(path)], user="root")
        self.ordered_names = structure.get_ordered_names()
        self.prepared = True

    def load_data(self, data, table_name):
        if not data:
            return
        path = "/" + table_name + ".tsv"
        for row in list(data):
            sorted_row = []
            for name in self.ordered_names:
                sorted_row.append(str(row.data[name]))

            str_data = '\t'.join(sorted_row)
            self.node.exec_in_container(["bash", "-c", "echo \"{row}\" >> {fname}".format(row=str_data, fname=path)], user='root')


class SourceExecutableCache(_SourceExecutableBase):

    def _get_cmd(self, path):
        return "cat {}".format(path)

    def compatible_with_layout(self, layout):
        return 'cache' not in layout.name


class SourceExecutableHashed(_SourceExecutableBase):

    def _get_cmd(self, path):
        return "cat - >/dev/null;cat {}".format(path)

    def compatible_with_layout(self, layout):
        return 'cache' in layout.name

class SourceHTTPBase(ExternalSource):

    PORT_COUNTER = 5555
    def get_source_str(self, table_name):
        self.http_port = SourceHTTPBase.PORT_COUNTER
        url = "{schema}://{host}:{port}/".format(schema=self._get_schema(), host=self.docker_hostname, port=self.http_port)
        SourceHTTPBase.PORT_COUNTER += 1
        return '''
            <http>
                <url>{url}</url>
                <format>TabSeparated</format>
                <credentials>
                    <user>foo</user>
                    <password>bar</password>
                </credentials>
                <headers>
                    <header>
                        <name>api-key</name>
                        <value>secret</value>
                    </header>
                </headers>
            </http>
        '''.format(url=url)

    def prepare(self, structure, table_name, cluster):
        self.node = cluster.instances[self.docker_hostname]
        path = "/" + table_name + ".tsv"
        self.node.exec_in_container(["bash", "-c", "touch {}".format(path)], user='root')

        script_dir = os.path.dirname(os.path.realpath(__file__))
        self.node.copy_file_to_container(os.path.join(script_dir, './http_server.py'), '/http_server.py')
        self.node.copy_file_to_container(os.path.join(script_dir, './fake_cert.pem'), '/fake_cert.pem')
        self.node.exec_in_container([
            "bash",
            "-c",
            "python2 /http_server.py --data-path={tbl} --schema={schema} --host={host} --port={port} --cert-path=/fake_cert.pem".format(
                tbl=path, schema=self._get_schema(), host=self.docker_hostname, port=self.http_port)
        ], detach=True)
        self.ordered_names = structure.get_ordered_names()
        self.prepared = True

    def load_data(self, data, table_name):
        if not data:
            return
        path = "/" + table_name + ".tsv"
        for row in list(data):
            sorted_row = []
            for name in self.ordered_names:
                sorted_row.append(str(row.data[name]))

            str_data = '\t'.join(sorted_row)
            self.node.exec_in_container(["bash", "-c", "echo \"{row}\" >> {fname}".format(row=str_data, fname=path)], user='root')


class SourceHTTP(SourceHTTPBase):
    def _get_schema(self):
        return "http"


class SourceHTTPS(SourceHTTPBase):
    def _get_schema(self):
        return "https"


class SourceRedis(ExternalSource):
    def __init__(
            self, name, internal_hostname, internal_port, docker_hostname, docker_port, user, password, storage_type
    ):
        super(SourceRedis, self).__init__(
            name, internal_hostname, internal_port, docker_hostname, docker_port, user, password
        )
        self.storage_type = storage_type

    def get_source_str(self, table_name):
        return '''
            <redis>
                <host>{host}</host>
                <port>{port}</port>
                <db_index>0</db_index>
                <storage_type>{storage_type}</storage_type>
            </redis>
        '''.format(
            host=self.docker_hostname,
            port=self.docker_port,
            storage_type=self.storage_type,  # simple or hash_map
        )

    def prepare(self, structure, table_name, cluster):
        self.client = redis.StrictRedis(host=self.internal_hostname, port=self.internal_port)
        self.prepared = True
        self.ordered_names = structure.get_ordered_names()

    def load_data(self, data, table_name):
        self.client.flushdb()
        for row in list(data):
            values = []
            for name in self.ordered_names:
                values.append(str(row.data[name]))
            print 'values: ', values
            if len(values) == 2:
                self.client.set(*values)
                print 'kek: ', self.client.get(values[0])
            else:
                self.client.hset(*values)

    def compatible_with_layout(self, layout):
        if (
            layout.is_simple and self.storage_type == "simple" or
            layout.is_complex and self.storage_type == "simple" and layout.name == "complex_key_hashed_one_key" or
            layout.is_complex and self.storage_type == "hash_map" and layout.name == "complex_key_hashed_two_keys"
        ):
            return True
        return False


class SourceAerospike(ExternalSource):
    def __init__(self, name, internal_hostname, internal_port,
                 docker_hostname, docker_port, user, password):
        ExternalSource.__init__(self, name, internal_hostname, internal_port,
                 docker_hostname, docker_port, user, password)
        self.namespace = "test"
        self.set = "test_set"

    def get_source_str(self, table_name):
        print("AEROSPIKE get source str")
        return '''
            <aerospike>
                <host>{host}</host>
                <port>{port}</port>
            </aerospike>
        '''.format(
            host=self.docker_hostname,
            port=self.docker_port,
        )

    def prepare(self, structure, table_name, cluster):
        config = {
            'hosts': [ (self.internal_hostname, self.internal_port) ]
        }
        self.client = aerospike.client(config).connect()
        self.prepared = True
        print("PREPARED AEROSPIKE")
        print(config)

    def compatible_with_layout(self, layout):
        print("compatible AEROSPIKE")
        return layout.is_simple

    def _flush_aerospike_db(self):
        keys = []

        def handle_record((key, metadata, record)):
            print("Handle record {} {}".format(key, record))
            keys.append(key)

        def print_record((key, metadata, record)):
            print("Print record {} {}".format(key, record))

        scan = self.client.scan(self.namespace, self.set)
        scan.foreach(handle_record)

        [self.client.remove(key) for key in keys]

    def load_kv_data(self, values):
        self._flush_aerospike_db()

        print("Load KV Data Aerospike")
        if len(values[0]) == 2:
            for value in values:
                key = (self.namespace, self.set, value[0])
                print(key)
                self.client.put(key, {"bin_value": value[1]}, policy={"key": aerospike.POLICY_KEY_SEND})
                assert self.client.exists(key)
        else:
            assert("VALUES SIZE != 2")

        # print(values)

    def load_data(self, data, table_name):
        print("Load Data Aerospike")
        # print(data)
