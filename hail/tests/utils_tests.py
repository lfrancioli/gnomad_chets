import unittest

from utils import *

hc = None


def setUpModule():
    global hc
    hc = HailContext()  # master = 'local[2]')


def tearDownModule():
    global hc
    hc.stop()
    hc = None


def test_vds_from_rows(rows, flat_schema, types):
    """

    :param rows: Data
    :param list of str flat_schema: Names for schema
    :param list of obj types: Types for schema
    :return: Fake VDS
    :rtype: VariantDataset
    """
    flat_schema.insert(0, 'v')
    types.insert(0, TVariant())
    schema = TStruct(flat_schema, types)
    for i, row in enumerate(rows):
        row['v'] = Variant.parse('1:{}:A:T'.format(i + 1000))
    return VariantDataset.from_table(KeyTable.parallelize(rows, schema, key='v'))


def filter_test_vds():
    """

    :return: VDS with some filters
    :rtype: VariantDataset
    """
    rows = [
        # Bi-allelic expected behavior
        {'v': Variant.parse('1:10000:A:T'),   'InbreedingCoeff': None, 'AS_FilterStatus': [[]],              'expected_filters': [],                        'expected_after_split': [[]]},
        {'v': Variant.parse('1:10001:A:T'),   'InbreedingCoeff': 0.0,  'AS_FilterStatus': [[]],              'expected_filters': [],                        'expected_after_split': [[]]},
        {'v': Variant.parse('1:10002:A:T'),   'InbreedingCoeff': -0.5, 'AS_FilterStatus': [[]],              'expected_filters': ['InbreedingCoeff'],       'expected_after_split': [['InbreedingCoeff']]},
        {'v': Variant.parse('1:10003:A:T'),   'InbreedingCoeff': -0.5, 'AS_FilterStatus': [['RF']],          'expected_filters': ['InbreedingCoeff', 'RF'], 'expected_after_split': [['InbreedingCoeff', 'RF']]},
        {'v': Variant.parse('1:10004:A:T'),   'InbreedingCoeff': 0.0,  'AS_FilterStatus': [['RF']],          'expected_filters': ['RF'],                    'expected_after_split': [['RF']]},
        {'v': Variant.parse('1:10005:A:T'),   'InbreedingCoeff': 0.0,  'AS_FilterStatus': [['RF', 'AC0']],   'expected_filters': ['RF', 'AC0'],             'expected_after_split': [['RF', 'AC0']]},

        # Multi-allelic expected behavior
        {'v': Variant.parse('2:10000:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [[], []],          'expected_filters': [],                               'expected_after_split': [[], []]},
        {'v': Variant.parse('2:10001:A:T,C'), 'InbreedingCoeff': -0.5, 'AS_FilterStatus': [[], []],          'expected_filters': ['InbreedingCoeff'],              'expected_after_split': [['InbreedingCoeff'], ['InbreedingCoeff']]},
        {'v': Variant.parse('2:10002:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [['RF'], []],      'expected_filters': [],                               'expected_after_split': [['RF'], []]},
        {'v': Variant.parse('2:10003:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [['RF'], ['RF']],  'expected_filters': ['RF'],                           'expected_after_split': [['RF'], ['RF']]},
        {'v': Variant.parse('2:10004:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [['RF'], ['AC0']], 'expected_filters': ['RF', 'AC0'],                    'expected_after_split': [['RF'], ['AC0']]},
        {'v': Variant.parse('2:10005:A:T,C'), 'InbreedingCoeff': -0.5, 'AS_FilterStatus': [['RF'], []],      'expected_filters': ['InbreedingCoeff'],              'expected_after_split': [['InbreedingCoeff', 'RF'], ['InbreedingCoeff']]},
        {'v': Variant.parse('2:10006:A:T,C'), 'InbreedingCoeff': -0.5, 'AS_FilterStatus': [['RF'], ['AC0']], 'expected_filters': ['InbreedingCoeff', 'RF', 'AC0'], 'expected_after_split': [['InbreedingCoeff', 'RF'], ['InbreedingCoeff', 'AC0']]},

        # Unexpected behavior
        {'v': Variant.parse('9:10000:A:T'),   'InbreedingCoeff': 0.0,  'AS_FilterStatus': None,              'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10001:A:T'),   'InbreedingCoeff': None, 'AS_FilterStatus': None,              'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10002:A:T'),   'InbreedingCoeff': -0.5, 'AS_FilterStatus': None,              'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10003:A:T'),   'InbreedingCoeff': 0.0,  'AS_FilterStatus': [None],            'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10004:A:T'),   'InbreedingCoeff': 0.0,  'AS_FilterStatus': [[None]],          'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10005:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [[], None],        'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10006:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [[], [None]],      'expected_filters': None,                      'expected_after_split': None},
        {'v': Variant.parse('9:10007:A:T,C'), 'InbreedingCoeff': 0.0,  'AS_FilterStatus': [['RF'], [None]],  'expected_filters': None,                      'expected_after_split': None},
    ]
    schema = ['v', 'InbreedingCoeff', 'AS_FilterStatus', 'expected_filters', 'expected_after_split']
    types = [TVariant(), TDouble(), TArray(TSet(TString())), TSet(TString()), TArray(TSet(TString()))]
    return VariantDataset.from_table(KeyTable.from_py(hc, rows, TStruct(schema, types), key_names=['v']))


class FilteringTests(unittest.TestCase):

    def test_allele_filtering(self):
        vds = filter_test_vds()

        site_filters = {
            'InbreedingCoeff': 'isDefined(va.InbreedingCoeff) && va.InbreedingCoeff < -0.3'
        }

        result_vds = set_site_filters(vds, site_filters, 'va.AS_FilterStatus')
        result_vds.filter_intervals(Interval.parse('9')).variants_table().show(50)
        result = result_vds.query_variants('variants.map(v => (isMissing(va.filters) && isMissing(va.expected_filters)) || va.filters == va.expected_filters).counter()')
        self.assertEqual(result[True], sum(result.values()))

        split_vds = result_vds.split_multi().annotate_variants_expr(index_into_arrays(['va.AS_FilterStatus', 'va.expected_after_split']))
        result_split_vds = set_site_filters(split_vds, site_filters, 'va.AS_FilterStatus')
        result_split_vds.variants_table().show(50)
        result = result_split_vds.query_variants('variants.map(v => va.filters == va.expected_after_split).counter()')
        self.assertEqual(result[True], sum(result.values()))


if __name__ == '__main__':
    unittest.main()