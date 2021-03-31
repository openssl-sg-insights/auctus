import contextlib
import csv
from datetime import datetime
from dateutil.tz import UTC
import io
import os
import pandas
import random
import requests
import tempfile
import textwrap
import unittest

import datamart_geo
from datamart_profiler import process_dataset
from datamart_profiler.core import expand_attribute_name, load_data
from datamart_profiler import profile_types
from datamart_profiler import spatial
from datamart_profiler.spatial import LATITUDE, LONGITUDE, LatLongColumn, \
    disambiguate_admin_areas
from datamart_profiler.temporal import get_temporal_resolution, parse_date

from .utils import DataTestCase, data


def check_ranges(min_, max_):
    def check(ranges):
        assert 2 <= len(ranges) <= 3
        for rg in ranges:
            assert rg.keys() == {'range'}
            rg = rg['range']
            assert rg.keys() == {'gte', 'lte'}
            gte, lte = rg['gte'], rg['lte']
            assert min_ <= gte <= lte <= max_

        return True

    return check


def check_geohashes(prefix):
    def check(geohashes):
        assert isinstance(geohashes, list)
        assert 3 <= len(geohashes) <= 100
        for entry in geohashes:
            assert entry.keys() == {'hash', 'number'}
            assert isinstance(entry['number'], int)
            assert entry['number'] > 0
            assert entry['hash'].startswith(prefix)

        return True

    return check


def check_geo_ranges(min_long, min_lat, max_long, max_lat):
    def check(ranges):
        assert len(ranges) == 3
        for rg in ranges:
            assert rg.keys() == {'range'}
            rg = rg['range']
            assert rg.keys() == {'type', 'coordinates'}
            assert rg['type'] == 'envelope'
            [long1, lat1], [long2, lat2] = rg['coordinates']
            assert min_lat <= lat2 <= lat1 <= max_lat
            assert min_long <= long1 <= long2 <= max_long

        return True

    return check


def check_plot(kind):
    def check(plot):
        return plot['type'] == kind

    return check


class TestCLI(unittest.TestCase):
    def test_size(self):
        from datamart_profiler.__main__ import parse_size

        self.assertEqual(parse_size('123'), 123)
        self.assertEqual(parse_size('123 B'), 123)
        self.assertEqual(parse_size('123B'), 123)
        self.assertEqual(parse_size('123k'), 123000)
        self.assertEqual(parse_size('123 k'), 123000)
        self.assertEqual(parse_size('123M'), 123000000)
        self.assertEqual(parse_size('123 M'), 123000000)


class TestSample(unittest.TestCase):
    @contextlib.contextmanager
    def random_data(self, rows):
        with tempfile.NamedTemporaryFile('w+') as tmp:
            writer = csv.writer(tmp)
            writer.writerow(['id', 'number'])
            rand = random.Random(4)
            for i in range(rows):
                writer.writerow([i, rand.randint(100000, 999999)])
            tmp.flush()
            filesize = os.stat(tmp.name).st_size
            yield tmp, filesize

    def test_sample(self):
        """Test sampling of tables"""
        with self.random_data(1000) as (tmp, filesize):
            self.assertEqual(filesize, 11901)
            data, data_path, metadata, column_names = load_data(tmp.name, 5000)
            self.assertEqual(data.shape, (421, 2))

        with self.random_data(600) as (tmp, filesize):
            self.assertEqual(filesize, 7101)
            data, data_path, metadata, column_names = load_data(tmp.name, 5000)
            self.assertEqual(data.shape, (423, 2))

        with self.random_data(425) as (tmp, filesize):
            self.assertEqual(filesize, 5001)
            data, data_path, metadata, column_names = load_data(tmp.name, 5000)
            self.assertEqual(data.shape, (425, 2))

            data, data_path, metadata, column_names = load_data(tmp.name, 6000)
            self.assertEqual(data.shape, (425, 2))


class TestNames(unittest.TestCase):
    def test_names(self):
        """Test expanding column names"""
        self.assertEqual(
            list(expand_attribute_name('Apt221bBakerStreet')),
            ['Apt', '221', 'b', 'Baker', 'Street'],
        )
        self.assertEqual(
            list(expand_attribute_name('place')),
            ['place'],
        )

    def test_duplicate_column_names(self):
        """Test reading a CSV with duplicate names"""
        metadata = process_dataset(io.StringIO(textwrap.dedent('''\
            one,two,one
            a,1,c
            d,2,f
        ''')))
        self.assertEqual(
            [col['name'] for col in metadata['columns']],
            ['one', 'two', 'one'],
        )


class TestIndex(unittest.TestCase):
    DATA = pandas.DataFrame({
        'a': [1, 1, 2, 2],
        'b': [3, 4, 5, 6],
        'c': [24, 35, 63, 57],
    })

    def test_no_index(self):
        """Test profiling a DataFrame that has no index, for reference"""
        df = self.DATA
        self.assertEqual(list(df.columns), ['a', 'b', 'c'])

        metadata = process_dataset(df)
        self.assertEqual(
            [col['name'] for col in metadata['columns']],
            ['a', 'b', 'c'],
        )

    def test_index(self):
        """Test profiling a DataFrame that has an index set"""
        df = self.DATA.set_index(['a'])
        self.assertEqual(list(df.index.names), ['a'])
        self.assertEqual(list(df.columns), ['b', 'c'])

        metadata = process_dataset(df)
        self.assertEqual(
            [col['name'] for col in metadata['columns']],
            ['a', 'b', 'c'],
        )

    def test_multi_index(self):
        """Test profiling a DataFrame that has multiple indexes (MultiIndex)"""
        df = self.DATA.set_index(['a', 'b'])
        self.assertEqual(list(df.index.names), ['a', 'b'])
        self.assertEqual(list(df.columns), ['c'])

        metadata = process_dataset(df)
        self.assertEqual(
            [col['name'] for col in metadata['columns']],
            ['a', 'b', 'c'],
        )


class TestLatlongSelection(DataTestCase):
    def test_normalize_name(self):
        """Test normalizing column names"""
        self.assertEqual(
            spatial.normalize_latlong_column_name('latitude', LATITUDE),
            '',
        )
        self.assertEqual(
            spatial.normalize_latlong_column_name('Place_Latitude', LATITUDE),
            'place_',
        )
        self.assertEqual(
            spatial.normalize_latlong_column_name('start_Long_deg', LONGITUDE),
            'start__deg',
        )
        self.assertEqual(
            spatial.normalize_latlong_column_name('start_Lon_deg', LONGITUDE),
            'start__deg',
        )

    def test_pairing(self):
        """Test pairing latitude and longitude columns by name or matching pairs defined by the user"""
        pairs, (missed_lat, missed_long) = spatial.pair_latlong_columns(
            [
                LatLongColumn(0, 'Pickup_latitude', None),
                LatLongColumn(1, 'lat', None),
                LatLongColumn(2, 'dropoff_latitude', None),
                LatLongColumn(3, 'latitude_place', None),
                LatLongColumn(4, 'la_coord', '1'),
            ],
            [
                LatLongColumn(5, 'long', None),
                LatLongColumn(6, 'dropoff_Longitude', None),
                LatLongColumn(7, 'pickup_longitude', None),
                LatLongColumn(8, 'other_Longitude', None),
                LatLongColumn(9, 'lo_coord', '1'),
            ],
        )
        self.assertEqual(
            pairs,
            [
                (LatLongColumn(1, 'lat', None),
                 LatLongColumn(5, 'long', None)),
                (LatLongColumn(2, 'dropoff_latitude', None),
                 LatLongColumn(6, 'dropoff_Longitude', None)),
                (LatLongColumn(0, 'Pickup_latitude', None),
                 LatLongColumn(7, 'pickup_longitude', None)),
                (LatLongColumn(4, 'la_coord', '1'),
                 LatLongColumn(9, 'lo_coord', '1')),
            ],
        )
        self.assertEqual(
            missed_lat,
            ['latitude_place'],
        )
        self.assertEqual(
            missed_long,
            ['other_Longitude'],
        )

    def test_process(self):
        """Test pairing latitudes & longitudes in profiler"""
        with data('lat_longs.csv', 'r') as data_fp:
            dataframe = pandas.read_csv(data_fp)
        metadata = process_dataset(
            dataframe,
        )
        # Check columns
        self.assertJson(
            [
                {k: v for k, v in c.items()
                 if k in ['name', 'structural_type', 'semantic_types']}
                for c in metadata['columns']],
            [
                {
                    'name': 'from latitude',
                    'structural_type': 'http://schema.org/Float',
                    'semantic_types': ['http://schema.org/latitude'],
                },
                {
                    'name': 'to long',
                    'structural_type': 'http://schema.org/Float',
                    'semantic_types': ['http://schema.org/longitude'],
                },
                {
                    'name': 'to lat',
                    'structural_type': 'http://schema.org/Float',
                    'semantic_types': ['http://schema.org/latitude'],
                },
                {
                    'name': 'from longitude',
                    'structural_type': 'http://schema.org/Float',
                    'semantic_types': ['http://schema.org/longitude'],
                },
                {
                    'name': 'unpaired lat',
                    'structural_type': 'http://schema.org/Float',
                    'semantic_types': [],
                },
            ]
        )
        # Check pairs
        self.assertJson(
            [
                {
                    k: v for k, v in c.items()
                    if k not in ('ranges', 'geohashes4', 'number')
                }
                for c in metadata['spatial_coverage']
            ],
            [
                {'type': 'latlong', 'column_names': ['to lat', 'to long'], 'column_indexes': [2, 1]},
                {'type': 'latlong', 'column_names': ['from latitude', 'from longitude'], 'column_indexes': [0, 3]},
            ],
        )


class TestDates(DataTestCase):
    def test_parse(self):
        """Test parsing dates"""
        self.assertEqual(
            parse_date('Monday July 1, 2019'),
            datetime(2019, 7, 1, tzinfo=UTC),
        )
        self.assertEqual(
            parse_date('20190702T211319Z'),
            datetime(2019, 7, 2, 21, 13, 19, tzinfo=UTC),
        )
        dt = parse_date('2019-07-02T21:13:19-04:00')
        self.assertEqual(
            dt.replace(tzinfo=None),
            datetime(2019, 7, 2, 21, 13, 19),
        )
        self.assertEqual(
            dt.astimezone(UTC),
            datetime(2019, 7, 3, 1, 13, 19, tzinfo=UTC),
        )

        # Check that unknown timezones are not accepted
        self.assertEqual(
            parse_date('2019-07-02 18:05 UTC'),
            datetime(2019, 7, 2, 18, 5, tzinfo=UTC),
        )
        self.assertEqual(
            parse_date('2019-07-02 18:05 L'),
            None,
        )

    def test_year(self):
        """Test the 'year' special-case"""
        dataframe = pandas.DataFrame({
            'year': [2004, 2005, 2006],
            'number': [2014, 2015, float('nan')],
        })
        metadata = process_dataset(dataframe)

        def year_rng(year):
            year = float(year)
            return {'range': {'gte': year, 'lte': year}}

        self.assertJson(
            metadata,
            {
                'nb_rows': 3,
                'nb_profiled_rows': 3,
                'nb_columns': 2,
                'nb_temporal_columns': 1,
                'nb_numerical_columns': 1,
                'types': ['numerical', 'temporal'],
                'attribute_keywords': ['year', 'number'],
                'columns': [
                    {
                        'name': 'year',
                        'structural_type': 'http://schema.org/Text',
                        'semantic_types': ['http://schema.org/DateTime'],
                        'unclean_values_ratio': 0.0,
                        'num_distinct_values': 3,
                    },
                    {
                        'name': 'number',
                        'structural_type': 'http://schema.org/Integer',
                        'semantic_types': [],
                        'missing_values_ratio': lambda n: round(n, 2) == 0.33,
                        'unclean_values_ratio': 0.0,
                        'num_distinct_values': 2,
                        'mean': 2014.5,
                        'stddev': 0.5,
                        'coverage': [
                            {'range': {'gte': 2014.0, 'lte': 2014.0}},
                            {'range': {'gte': 2015.0, 'lte': 2015.0}},
                        ],
                    },
                ],
                'temporal_coverage': [
                    {
                        'type': 'datetime',
                        'column_names': ['year'],
                        'column_indexes': [0],
                        'column_types': ['http://schema.org/DateTime'],
                        'ranges': [
                            year_rng(1072915200.0),
                            year_rng(1104537600.0),
                            year_rng(1136073600.0),
                        ],
                        'temporal_resolution': 'year',
                    },
                ]
            },
        )


class TestTemporalResolutions(unittest.TestCase):
    def test_pandas(self):
        """Test guessing temporal resolution of Pandas values"""
        def get_res(values):
            idx = pandas.Index(pandas.to_datetime(values))
            return get_temporal_resolution(idx)

        self.do_checks(get_res)

    def test_native(self):
        """Test guessing temporal resolution of native Python values"""
        def get_res(values):
            values = [parse_date(d) for d in values]
            return get_temporal_resolution(values)

        self.do_checks(get_res)

    def do_checks(self, get_res):
        self.assertEqual(
            get_res([
                '2020-01-14T21:05:02',
                '2020-01-14T21:05:07',
                '2020-01-14T21:05:30',
                '2020-01-14T22:21:54',
                '2020-01-15T05:12:41',
                '2020-03-21T05:12:41',
            ]),
            'second',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T21:05:02',
                '2020-01-14T21:05:07',
                '2020-01-14T21:05:30',
            ]),
            'second',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T21:05:00Z',
                '2020-01-14T21:05:00Z',
                '2020-01-14T21:05:00Z',
                '2020-01-14T21:21:00Z',
                '2020-01-15T05:12:00Z',
                '2020-03-21T05:12:00Z',
                '2020-03-21T05:13:00Z',
            ]),
            'minute',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T21:30:00',
                '2020-01-14T21:30:00',
                '2020-01-14T21:30:00',
                '2020-01-14T22:31:07',
                '2020-01-15T05:29:59',
                '2020-03-21T05:29:59',
            ]),
            'hour',
        )
        self.assertEqual(
            get_res([
                '2020-01-14',
                '2020-01-14',
                '2020-01-14',
                '2020-01-14',
                '2020-01-15',
                '2020-03-21',
            ]),
            'day',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T05:12:41',
                '2020-01-14T05:12:41',
                '2020-01-14T05:12:41',
                '2020-01-14T05:12:41',
                '2020-01-15T05:13:03',
                '2020-01-16T05:12:41',
                '2020-03-21T05:12:41',
            ]),
            'day',
        )
        self.assertEqual(
            get_res([
                '2020-01-30',
                '2020-01-30',
                '2020-02-13',
                '2020-02-21',
                '2020-03-05',
                '2020-03-05',
                '2020-03-12',
            ]),
            'week',
        )
        self.assertEqual(
            get_res([
                '2020-02-06',
                '2020-02-06',
                '2020-03-07',
                '2020-05-07',
                '2020-06-06',
            ]),
            'month',
        )
        self.assertEqual(
            get_res([
                '2017-02-06',
                '2018-03-06',
                '2018-03-06',
                '2020-02-04',
            ]),
            'year',
        )
        self.assertEqual(
            get_res([
                '2017-01-01',
                '2018-04-01',
                '2018-07-01',
                '2018-12-31',
            ]),
            'quarter',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T05:12:41',
                '2020-01-14T05:12:41',
            ]),
            'second',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T05:12:00',
                '2020-01-14T05:12:00',
            ]),
            'minute',
        )
        self.assertEqual(
            get_res([
                '2020-01-14T00:00:00',
                '2020-01-14T00:00:00',
            ]),
            'day',
        )
        self.assertEqual(
            get_res([
                '2020-12',
                '2020-12',
            ]),
            'day',
        )


class TestTypes(unittest.TestCase):
    def do_test(self, match, positive, negative):
        for elem in textwrap.dedent(positive).splitlines():
            if elem:
                self.assertTrue(match(elem),
                                "Didn't match: %s" % elem)
        for elem in textwrap.dedent(negative).splitlines():
            if elem:
                self.assertFalse(match(elem),
                                 "Shouldn't have matched: %s" % elem)

    def test_ints(self):
        """Test the integer type detection"""
        positive = '''\
        12
        0
        +478
        -17
        '''
        negative = '''\
        1.7
        7A
        ++2
        --34
        +-7
        -+18
        '''
        self.do_test(
            profile_types._re_int.match,
            positive, negative,
        )
        self.assertFalse(profile_types._re_int.match(''))

    def test_floats(self):
        """Test the floating-point number type detection"""
        positive = '''\
        12.
        0.
        .7
        123.456
        +123.456
        +.456
        -.4
        .4e17
        -.4e17
        +8.4e17
        +8.e17
        '''
        negative = '''\
        1.7.3
        .7.3
        7.3.
        .
        -.
        +.
        7.A
        .e8
        8e17
        1.3e
        '''
        self.do_test(
            profile_types._re_float.match,
            positive, negative,
        )
        self.assertFalse(profile_types._re_float.match(''))

    def test_urls(self):
        """Test the URL type detection"""
        positive = '''\
        http://en.wikipedia.org/wiki/Data_mart
        https://auctus.vida-nyu.org/
        ftp://docs.auctus.vida-nyu.org/master/
        '''
        negative = '''\
        auctus.vida-nyu.org
        auctus
        data.mart
        '''
        self.do_test(
            profile_types._re_url.match,
            positive, negative,
        )
        self.assertFalse(profile_types._re_url.match(''))

    def test_filenames(self):
        """Test the file path type detection"""
        positive = '''\
        /home/remram/projects/auctus/auctus/tests
        /var/mail/fchirigati
        /Applications/VIDA/Auctus.app
        /opt/reprounzip
        /Library/Frameworks/Python.framework/Versions/3.7/bin/python3.7
        C:\\Program Files\\ReproUnzip
        C:\\Python3.7\\python.exe
        file:////tmp/pipelines.sqlite3
        '''
        negative = '''\
        /nan/
        C: answer C
        D: obiwan kenobi
        '''
        self.do_test(
            profile_types._re_file.match,
            positive, negative,
        )
        self.assertFalse(profile_types._re_file.match(''))

    def test_geo_combined(self):
        """Test the "combined" geo point type detection"""
        positive = '''\
        70 WASHINGTON SQUARE S, NEW YORK, NY 10012 (40.729753, -73.997174)
        R\u00C9MI'S HOUSE, BROOKLYN, NY (40.729753, -73.997174)
        '''
        negative = '''\
        70 Washington Square (40.729753, -73.997174)
        '''
        self.do_test(
            profile_types._re_geo_combined.match,
            positive, negative,
        )


class TestTruncate(unittest.TestCase):
    def test_simple(self):
        """Test truncating a string"""
        from datamart_profiler.core import truncate_string

        self.assertEqual(truncate_string("abc", 10), "abc")
        self.assertEqual(truncate_string("abcdefghij", 10), "abcdefghij")
        self.assertEqual(truncate_string("abcdefghijk", 10), "abcdefg...")
        self.assertEqual(truncate_string("abcdefghijklmnop", 10), "abcdefg...")

    def test_words(self):
        """Test that truncating a string prefers a word boundary"""
        from datamart_profiler.core import truncate_string

        self.assertEqual(
            truncate_string("abcde fghijklmnopqrs tuvwxyzABCD EF", 30),
            "abcde fghijklmnopqrs...",
        )
        self.assertEqual(
            truncate_string("abcde fghijklmnopqrs tu vwxyzABCD EF", 30),
            "abcde fghijklmnopqrs tu...",
        )
        self.assertEqual(
            truncate_string("abc defghijklmnopqrstuvwxyzABCDEFGHI", 30),
            "abc defghijklmnopqrstuvwxyz...",
        )


class TestNominatim(DataTestCase):
    """Test resolving addresses, mocking Nominatim queries"""
    def test_profile(self):
        queries = {
            "70 Washington Square S, New York, NY 10012": [{
                'lat': 40.7294, 'lon': -73.9972,
            }],
            "6 MetroTech, Brooklyn, NY 11201": [{
                'lat': 40.6944, 'lon': -73.9857,
            }],
            "251 Mercer St, New York, NY 10012": [{
                'lat': 40.7287, 'lon': -73.9957,
            }],
        }

        def replacement(url, *, q):
            if not replacement.failed:  # Fail just once
                replacement.failed = True
                response = requests.Response()
                response.status_code = 500
                raise requests.HTTPError("Fake 500 error", response=response)
            return [queries[qe] for qe in q]
        replacement.failed = False

        old_query = spatial.nominatim_query
        old_min_batch_size = spatial.NOMINATIM_MIN_SPLIT_BATCH_SIZE
        spatial.nominatim_query = replacement
        spatial.NOMINATIM_MIN_SPLIT_BATCH_SIZE = 2
        try:
            with data('addresses.csv', 'r') as data_fp:
                metadata = process_dataset(
                    data_fp,
                    nominatim='http://nominatim/',
                    coverage=True,
                )
        finally:
            spatial.nominatim_query = old_query
            spatial.NOMINATIM_MIN_SPLIT_BATCH_SIZE = old_min_batch_size

        self.assertJson(
            metadata,
            {
                'size': 142,
                'nb_rows': 3,
                'nb_profiled_rows': 3,
                'nb_columns': 2,
                'nb_spatial_columns': 1,
                'average_row_size': lambda n: round(n, 2) == 47.33,
                'types': ['spatial'],
                'attribute_keywords': ['place', 'loc'],
                'columns': [
                    {
                        'name': 'place',
                        'num_distinct_values': 3,
                        'structural_type': 'http://schema.org/Text',
                        'semantic_types': [],
                    },
                    {
                        'name': 'loc',
                        'structural_type': 'http://schema.org/Text',
                        'semantic_types': [
                            'http://schema.org/Text',
                            'http://schema.org/address',
                        ],
                    },
                ],
                'spatial_coverage': [
                    {
                        'type': 'address',
                        'column_names': ['loc'],
                        'column_indexes': [1],
                        'ranges': check_geo_ranges(-74.00, 40.69, -73.98, 40.73),
                        'geohashes4': [
                            {'hash': '1211302313301103', 'number': 1},
                            {'hash': '1211302313301102', 'number': 1},
                            {'hash': '1211302313300022', 'number': 1},
                        ],
                        'number': 3,
                    },
                ],
            },
        )

    def test_querying(self):
        """Test Nominatim internals, mocking the queries"""
        queries = {
            'a': [{'lat': 11.0, 'lon': 12.0}],
            'b': [],
            'c': [{'lat': 31.0, 'lon': 32.0}],
        }
        old_query = spatial.nominatim_query
        spatial.nominatim_query = \
            lambda url, *, q: [queries[qe] for qe in q]
        try:
            res, empty = spatial.nominatim_resolve_all(
                'http://240.123.45.67:21',
                ['a', 'b', 'c', 'b', 'b', 'c'],
            )
            self.assertEqual(
                res,
                [
                    (11.0, 12.0),
                    (31.0, 32.0),
                    (31.0, 32.0),
                ],
            )
            self.assertEqual(empty, 6)

            res, empty = spatial.nominatim_resolve_all(
                'http://240.123.45.67:21',
                [
                    'a', 'b', 'c', 'b', 'b', 'c', 'a', 'b', 'c',
                    # Second batch
                    'b', 'b', 'c',
                ],
            )
            self.assertEqual(
                res,
                [
                    (11.0, 12.0),
                    (11.0, 12.0),
                    (31.0, 32.0),
                    (31.0, 32.0),
                    (31.0, 32.0),
                    # Second batch
                    (31.0, 32.0),
                ],
            )
            self.assertEqual(empty, 12)
        finally:
            spatial.nominatim_query = old_query


class TestGeo(DataTestCase):
    @classmethod
    def setUpClass(cls):
        cls.geo_data = datamart_geo.GeoData.from_local_cache()

    def test_admin(self):
        """Test profiling administrative areas"""
        with data('admins.csv', 'r') as data_fp:
            metadata = process_dataset(
                data_fp,
                geo_data=self.geo_data,
                coverage=True,
            )

        self.assertJson(
            metadata,
            {
                'size': 145,
                'nb_rows': 5,
                'nb_profiled_rows': 5,
                'nb_columns': 3,
                'nb_spatial_columns': 2,
                'average_row_size': lambda n: round(n, 2) == 29.0,
                'types': ['spatial'],
                'attribute_keywords': ['zero', 'one', 'mixed'],
                'columns': [
                    {
                        'name': 'zero',
                        'structural_type': 'http://schema.org/Text',
                        'semantic_types': [
                            'http://schema.org/AdministrativeArea',
                            'http://schema.org/Enumeration',
                        ],
                        'num_distinct_values': 3,
                        'admin_area_level': 0,
                    },
                    {
                        'name': 'one',
                        'structural_type': 'http://schema.org/Text',
                        'semantic_types': [
                            'http://schema.org/AdministrativeArea',
                            'http://schema.org/Enumeration',
                        ],
                        'num_distinct_values': 5,
                        'admin_area_level': 1,
                    },
                    {
                        'name': 'mixed',
                        'structural_type': 'http://schema.org/Text',
                        'semantic_types': [],
                        'num_distinct_values': 5,
                    },
                ],
                'spatial_coverage': [
                    {
                        'type': 'admin',
                        'column_names': ['zero'],
                        'column_indexes': [0],
                        'ranges': [
                            {
                                'range': {
                                    'type': 'envelope',
                                    'coordinates': [
                                        [-61.797841, 55.065334],
                                        [55.854503, -21.370782],
                                    ],
                                },
                            },
                        ],
                        'geohashes4': lambda l: sorted(l, key=lambda h: h['hash']) == [
                            {'hash': '0312', 'number': 1},
                            {'hash': '0313', 'number': 1},
                            {'hash': '0330', 'number': 1},
                            {'hash': '0331', 'number': 1},
                            {'hash': '0332', 'number': 1},
                            {'hash': '0333', 'number': 1},
                            {'hash': '1202', 'number': 1},
                            {'hash': '1203', 'number': 1},
                            {'hash': '1212', 'number': 1},
                            {'hash': '1213', 'number': 1},
                            {'hash': '1220', 'number': 1},
                            {'hash': '1221', 'number': 1},
                            {'hash': '1222', 'number': 1},
                            {'hash': '1223', 'number': 1},
                            {'hash': '1230', 'number': 1},
                            {'hash': '1231', 'number': 1},
                            {'hash': '1232', 'number': 2},
                            {'hash': '1233', 'number': 2},
                            {'hash': '1302', 'number': 1},
                            {'hash': '1320', 'number': 1},
                            {'hash': '1322', 'number': 1},
                            {'hash': '2110', 'number': 1},
                            {'hash': '2111', 'number': 1},
                            {'hash': '2112', 'number': 1},
                            {'hash': '2113', 'number': 1},
                            {'hash': '2130', 'number': 1},
                            {'hash': '2131', 'number': 1},
                            {'hash': '3000', 'number': 1},
                            {'hash': '3001', 'number': 1},
                            {'hash': '3002', 'number': 1},
                            {'hash': '3003', 'number': 1},
                            {'hash': '3010', 'number': 2},
                            {'hash': '3011', 'number': 2},
                            {'hash': '3012', 'number': 1},
                            {'hash': '3013', 'number': 1},
                            {'hash': '3020', 'number': 1},
                            {'hash': '3021', 'number': 1},
                            {'hash': '3030', 'number': 1},
                            {'hash': '3031', 'number': 1},
                            {'hash': '3100', 'number': 2},
                            {'hash': '3102', 'number': 1},
                            {'hash': '3120', 'number': 1},
                        ],
                        'number': 3,
                    },
                    {
                        'type': 'admin',
                        'column_names': ['one'],
                        'column_indexes': [1],
                        'ranges': [
                            {
                                'range': {
                                    'type': 'envelope',
                                    'coordinates': [
                                        [8.97659, 50.56286],
                                        [13.81686, 47.27112],
                                    ],
                                },
                            },
                        ],
                        'geohashes4': lambda l: sorted(l, key=lambda h: h['hash']) == [
                            {'hash': '310002310', 'number': 1},
                            {'hash': '310002311', 'number': 1},
                            {'hash': '310002312', 'number': 1},
                            {'hash': '310002313', 'number': 1},
                            {'hash': '310002330', 'number': 1},
                            {'hash': '310002331', 'number': 1},
                            {'hash': '310002332', 'number': 1},
                            {'hash': '310002333', 'number': 1},
                            {'hash': '310003200', 'number': 1},
                            {'hash': '310003201', 'number': 1},
                            {'hash': '310003202', 'number': 1},
                            {'hash': '310003203', 'number': 1},
                            {'hash': '310003210', 'number': 1},
                            {'hash': '310003211', 'number': 1},
                            {'hash': '310003212', 'number': 1},
                            {'hash': '310003213', 'number': 1},
                            {'hash': '310003220', 'number': 1},
                            {'hash': '310003221', 'number': 1},
                            {'hash': '310003222', 'number': 1},
                            {'hash': '310003223', 'number': 1},
                            {'hash': '310003230', 'number': 1},
                            {'hash': '310003231', 'number': 1},
                            {'hash': '310003232', 'number': 1},
                            {'hash': '310003233', 'number': 1},
                            {'hash': '310003300', 'number': 1},
                            {'hash': '310003301', 'number': 1},
                            {'hash': '310003302', 'number': 1},
                            {'hash': '310003303', 'number': 1},
                            {'hash': '310003310', 'number': 1},
                            {'hash': '310003311', 'number': 1},
                            {'hash': '310003312', 'number': 1},
                            {'hash': '310003313', 'number': 1},
                            {'hash': '310003320', 'number': 1},
                            {'hash': '310003321', 'number': 1},
                            {'hash': '310003322', 'number': 1},
                            {'hash': '310003323', 'number': 1},
                            {'hash': '310003330', 'number': 1},
                            {'hash': '310003331', 'number': 1},
                            {'hash': '310003332', 'number': 1},
                            {'hash': '310003333', 'number': 1},
                            {'hash': '310020110', 'number': 1},
                            {'hash': '310020111', 'number': 1},
                            {'hash': '310020112', 'number': 1},
                            {'hash': '310020113', 'number': 1},
                            {'hash': '310020130', 'number': 1},
                            {'hash': '310020131', 'number': 1},
                            {'hash': '310020132', 'number': 1},
                            {'hash': '310020133', 'number': 1},
                            {'hash': '310021000', 'number': 1},
                            {'hash': '310021001', 'number': 1},
                            {'hash': '310021002', 'number': 1},
                            {'hash': '310021003', 'number': 1},
                            {'hash': '310021010', 'number': 1},
                            {'hash': '310021011', 'number': 1},
                            {'hash': '310021012', 'number': 1},
                            {'hash': '310021013', 'number': 1},
                            {'hash': '310021020', 'number': 1},
                            {'hash': '310021021', 'number': 1},
                            {'hash': '310021022', 'number': 1},
                            {'hash': '310021023', 'number': 1},
                            {'hash': '310021030', 'number': 1},
                            {'hash': '310021031', 'number': 1},
                            {'hash': '310021032', 'number': 1},
                            {'hash': '310021033', 'number': 1},
                            {'hash': '310021100', 'number': 1},
                            {'hash': '310021101', 'number': 1},
                            {'hash': '310021102', 'number': 1},
                            {'hash': '310021103', 'number': 1},
                            {'hash': '310021110', 'number': 1},
                            {'hash': '310021111', 'number': 1},
                            {'hash': '310021112', 'number': 1},
                            {'hash': '310021113', 'number': 1},
                            {'hash': '310021120', 'number': 1},
                            {'hash': '310021121', 'number': 1},
                            {'hash': '310021122', 'number': 1},
                            {'hash': '310021123', 'number': 1},
                            {'hash': '310021130', 'number': 1},
                            {'hash': '310021131', 'number': 1},
                            {'hash': '310021132', 'number': 1},
                            {'hash': '310021133', 'number': 1},
                        ],
                        # FIXME: number currently 1 because of missing geo data
                        'number': lambda n: isinstance(n, int),
                    },
                ],
            },
        )

    def test_admin_ambiguous(self):
        """Test the resolution of ambiguous administrative areas"""
        def countries(areas):
            if areas and isinstance(areas[0], (list, set)):
                # Flatten
                areas = [area for candidates in areas for area in candidates]
            return {
                area.get_parent_area(datamart_geo.Type.COUNTRY).name
                for area in areas
            }

        # 'SC' resolves to states in both USA and Brazil
        sc_states = [
            area
            for area in self.geo_data.resolve_name_all('SC')
            if area.type == datamart_geo.Type.ADMIN_1
        ]
        if countries(sc_states) <= {
            'Federative Republic of Brazil', 'United States',
        }:
            raise ValueError("assumptions about geo data are wrong")
        # 'CT' resolves to states in both USA and India
        ct_states = [
            area
            for area in self.geo_data.resolve_name_all('CT')
            if area.type == datamart_geo.Type.ADMIN_1
        ]
        if countries(ct_states) <= {'Republic of India', 'United States'}:
            raise ValueError("assumptions about geo data are wrong")

        # Test that resolving those state names resolves to the USA
        resolved_areas = self.geo_data.resolve_names_all(['SC', 'CT', 'NY'])
        self.assertTrue(
            countries(resolved_areas) >= {
                'United States', 'Federative Republic of Brazil',
                'Republic of India',
            },
        )
        level, resolved_areas = disambiguate_admin_areas(resolved_areas)
        self.assertEqual(
            countries(resolved_areas),
            {'United States'},
        )
        self.assertEqual(level, 1)

    def test_point_wkt(self):
        """Test profiling WKT points"""
        with data('geo_wkt.csv', 'r') as data_fp:
            metadata = process_dataset(
                data_fp,
                coverage=True,
            )

        self.assertJson(
            metadata,
            {
                'types': ['numerical', 'spatial'],
                "size": 4708,
                "nb_rows": 100,
                "nb_profiled_rows": 100,
                "nb_columns": 3,
                "nb_spatial_columns": 1,
                "nb_numerical_columns": 1,
                "average_row_size": lambda n: round(n, 2) == 47.08,
                "attribute_keywords": ["id", "coords", "height"],
                "columns": [
                    {
                        "name": "id",
                        "structural_type": "http://schema.org/Text",
                        "semantic_types": [],
                        "missing_values_ratio": 0.01,
                        "num_distinct_values": 99
                    },
                    {
                        "name": "coords",
                        "structural_type": "http://schema.org/GeoCoordinates",
                        "semantic_types": [],
                        "unclean_values_ratio": 0.0,
                        "point_format": "long,lat",
                    },
                    {
                        "name": "height",
                        "structural_type": "http://schema.org/Float",
                        "semantic_types": [],
                        "unclean_values_ratio": 0.0,
                        "mean": lambda n: round(n, 3) == 47.827,
                        "stddev": lambda n: round(n, 2) == 21.28,
                        "coverage": check_ranges(1.0, 90.0),
                    }
                ],
                "spatial_coverage": [
                    {
                        "type": "point",
                        "column_names": ["coords"],
                        "column_indexes": [1],
                        "geohashes4": check_geohashes('1211302313'),
                        "ranges": check_geo_ranges(-74.006, 40.6905, -73.983, 40.7352),
                        "number": 100,
                    }
                ],
            },
        )

    def test_point_latlong(self):
        """Test profiling latitudes & longitudes"""
        with data('geo_latlong.csv', 'r') as data_fp:
            metadata = process_dataset(
                data_fp,
                coverage=True,
            )

        self.assertJson(
            metadata,
            {
                'types': ['numerical', 'spatial'],
                "size": 4408,
                "nb_rows": 100,
                "nb_profiled_rows": 100,
                "nb_columns": 3,
                "nb_spatial_columns": 1,
                "nb_numerical_columns": 1,
                "average_row_size": lambda n: round(n, 2) == 44.08,
                "attribute_keywords": ["id", "coords", "height"],
                "columns": [
                    {
                        "name": "id",
                        "structural_type": "http://schema.org/Text",
                        "semantic_types": [],
                        "missing_values_ratio": 0.01,
                        "num_distinct_values": 99
                    },
                    {
                        "name": "coords",
                        "structural_type": "http://schema.org/GeoCoordinates",
                        "semantic_types": [],
                        "unclean_values_ratio": 0.0,
                        "point_format": "lat,long",
                    },
                    {
                        "name": "height",
                        "structural_type": "http://schema.org/Float",
                        "semantic_types": [],
                        "unclean_values_ratio": 0.0,
                        "mean": lambda n: round(n, 3) == 47.827,
                        "stddev": lambda n: round(n, 2) == 21.28,
                        "coverage": check_ranges(1.0, 90.0),
                    }
                ],
                "spatial_coverage": [
                    {
                        "type": "point_latlong",
                        "column_names": ["coords"],
                        "column_indexes": [1],
                        "geohashes4": check_geohashes('1211302313'),
                        "ranges": check_geo_ranges(-74.006, 40.6905, -73.983, 40.7352),
                        "number": 100,
                    },
                ],
            },
        )


class TestGeoHash(unittest.TestCase):
    def test_bit_encoding(self):
        self.assertEqual(
            spatial.bits_to_chars(
                [0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 1, 0, 1],
                base_bits=5,
            ),
            'dr5',
        )
        self.assertEqual(
            spatial.bits_to_chars(
                [1, 1, 0, 1, 0, 0, 0, 0, 0, 0],
                base_bits=2,
            ),
            '31000',
        )

    def test_bit_decoding(self):
        self.assertEqual(
            list(spatial.chars_to_bits('dr5', base_bits=5)),
            [0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 1, 0, 1],
        )
        self.assertEqual(
            list(spatial.chars_to_bits('31000', base_bits=2)),
            [1, 1, 0, 1, 0, 0, 0, 0, 0, 0],
        )

    def test_bitrange(self):
        results = []
        for bits in spatial.bitrange(
            [1, 0, 1, 0, 1, 0, 1],
            [1, 0, 1, 1, 0, 1, 0],
        ):
            results.append(''.join(str(b) for b in bits))
        self.assertEqual(
            results,
            [
                '1010101',
                '1010110',
                '1010111',
                '1011000',
                '1011001',
                '1011010',
            ],
        )

    def test_geohash32(self):
        self.assertEqual(
            spatial.hash_location((40.6962574, -73.9849621)),
            'dr5rs2tbckjz3h8c',
        )
        self.assertEqual(
            spatial.hash_location(
                (48.8588376, 2.2768489),
                base=32, precision=5,
            ),
            'u09tg',
        )

    def test_geohash32_reverse(self):
        self.assertEqual(
            spatial.decode_hash('dr5rs2tbckjz3h8c'),
            (
                40.696257399849856, 40.696257400013565,
                -73.98496210011217, -73.98496209978475,
            ),
        )
        self.assertEqual(
            spatial.decode_hash('u09tg', base=32),
            (
                48.8232421875, 48.8671875,
                2.2412109375, 2.28515625,
            ),
        )

    def test_geohash4(self):
        self.assertEqual(
            spatial.hash_location(
                (40.6962574, -73.9849621),
                base=4,
            ),
            '1211302313300023',
        )
        self.assertEqual(
            spatial.hash_location(
                (48.8588376, 2.2768489),
                base=4, precision=5,
            ),
            '31000',
        )

    def test_geohash4_reverse(self):
        self.assertEqual(
            spatial.decode_hash('1211302313300023', base=4),
            (
                40.69610595703125, 40.6988525390625,
                -73.9874267578125, -73.98193359375,
            ),
        )
        self.assertEqual(
            spatial.decode_hash('31000', base=4),
            (
                45.0, 50.625,
                0.0, 11.25,
            ),
        )

    def test_sketch_points(self):
        test_data = [
            ((40.0, 10.0), '3011'),
            ((20.0, 12.0), '3001'),
            ((30.0, 50.0), '3030'),
            ((18.0, 80.0), '3023'),
            ((15.0, -170.0), '1001'),
            ((14.0, -168.0), '1001'),
            ((-75.0, -50.0), '0203'),
            ((-76.0, -18.0), '0223'),
            ((-74.0, -16.0), '0223'),
            ((-75.0, -17.0), '0223'),
        ]
        points = [p[0] for p in test_data]
        self.assertEqual(
            [
                spatial.hash_location(point, base=4, precision=4)
                for point in points
            ],
            [p[1] for p in test_data],
        )
        builder = spatial.Geohasher(
            base=4,
            precision=4,  # Big enough it can fail, low enough I can debug
            number=3,
        )
        builder.add_points(points)
        self.assertEqual(
            builder.get_hashes(),
            [('30', 4), ('10', 2), ('02', 4)],
        )

        builder = spatial.Geohasher(
            base=4,
            precision=3,
            number=3,
        )
        builder.add_points([
            (1.0, 1.0), (46.0, 91.0), (44.0, 91.0), (89.0, 89.0),
        ])
        self.assertEqual(
            builder.get_hashes(),
            [('3', 4)],
        )

        builder = spatial.Geohasher(
            base=4,
            precision=3,
            number=3,
        )
        builder.add_points([
            (12.0, 12.0), (12.0, -12.0), (-12.0, -12.0), (-12.0, 12.0),
        ])
        self.assertEqual(
            builder.get_hashes(),
            [('', 4)],
        )

    def test_sketch_aab(self):
        builder = spatial.Geohasher(
            base=4,
            precision=4,
            number=20,
        )
        builder.add_aab((-100.0, -30.0, -15.0, 50.0))
        builder.add_aab((-80.0, 20.0, -50.0, 15.0))
        self.assertEqual(
            builder.get_hashes(),
            [
                ('013', 1),
                ('031', 2),
                ('033', 2),
                ('030', 1),
                ('032', 1),
                ('021', 1),
                ('023', 1),
                ('102', 1),
                ('103', 1),
                ('112', 1),
                ('120', 2),
                ('121', 1),
                ('122', 2),
                ('123', 1),
                ('130', 1),
                ('132', 1),
                ('201', 1),
                ('210', 1),
                ('211', 1),
                ('300', 1),
            ],
        )


class TestMedianDist(unittest.TestCase):
    def test_median_dist(self):
        """Test determining the median distance of points"""
        points = []

        def make_grid(mx, my):
            for y in range(-100, 100):
                for x in range(-100, 100):
                    points.append((
                        mx + x + random.random() * 0.2,
                        my + y + random.random() * 0.2,
                    ))
        make_grid(0, 0)
        make_grid(500, 0)
        make_grid(-200, 300)

        self.assertAlmostEqual(
            spatial.median_smallest_distance(points),
            0.9,
            delta=0.05
        )
