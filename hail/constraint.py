from utils import *
import argparse

# Temporary
import hail
from hail.expr import *
from hail.representation import *
final_exome_vds = 'gs://gnomad-public/release-170228/gnomad.exomes.r2.0.1.sites.vds'
final_genome_vds = 'gs://gnomad-public/release-170228/gnomad.genomes.r2.0.1.sites.vds'

hc = hail.HailContext(log="/hail.log")

fasta_path = "gs://gnomad-resources/Homo_sapiens_assembly19.fasta"
context_vds_path = "gs://gnomad-resources/Homo_sapiens_assembly19.fasta.vds"
mega_annotations_path = 'gs://hail-common/annotation_v0.2.vds'
mutation_rate_table_path = 'gs://gnomad-resources/fordist_1KG_mutation_rate_table.txt'
mutation_rate_kt_path = 'gs://gnomad-resources/mutation_rate.kt'


def import_fasta(fasta_path, output_vds_path):
    vds = hc.import_fasta(fasta_path,
                          filter_Ns=True, flanking_context=3, create_snv_alleles=True, create_deletion_size=2,
                          create_insertion_size=2, line_limit=2000)
    # TODO: VEP here? Or annotate with annotation VDS
    vds.write(output_vds_path, overwrite=True)


def load_mutation_rate():
    """
    :return: Mutation rate keytable
    :rtype: KeyTable
    """
    kt = hc.import_keytable(mutation_rate_table_path, config=hail.TextTableConfig(impute=True, delimiter=' '))
    return (kt.rename({'from': 'context', 'mu_snp': 'mutation_rate'})
            .annotate(['ref = context[2]', 'alt = to[2]']).key_by(['context', 'ref', 'alt']))


def count_variants(vds, criteria=None, additional_groupings=None, trimer=False, explode=None):
    """
    Counts variants in VDS by context, ref, alt, and any other groupings provided

    :param VariantDataset vds: Input VDS
    :param str criteria: Any filtering criteria (e.g. non-coding, non-conserved regions), to be passed to filter_variants_expr
    :param additional_groupings: Whether to group further (e.g. by functional annotation)
    :type additional_groupings: str or list of str
    :param bool trimer: whether to use trimer context (default heptamer)
    :param str explode: criteria to explode by (most likely va.vep.transcript_consequences)
    :return: keytable with counts as `variant_count`
    :rtype: KeyTable
    """
    if criteria is not None:
        vds = vds.filter_variants_expr(criteria)

    if trimer:
        vds = vds.annotate_variants_expr('va.context = va.context[2:5]')

    grouping = ['context = `va.context`', 'ref = v.ref', 'alt = v.alt']  # va gets flattened, so this is a little awkward
    if additional_groupings is not None:
        if type(additional_groupings) == str:
            grouping.append(additional_groupings)
        else:
            grouping.extend(additional_groupings)

    kt = vds.variants_keytable().flatten()

    if explode:
        kt = kt.explode(explode).flatten()

    return kt.aggregate_by_key(grouping, 'variant_count = v.count()')

    # data = context_kt.to_pandas()
    # data[(data.context.str.len() == 3) & (data.ref.str.len() == 1) & (data.alt.str.len() == 1) & (~data.context.str.contains('N'))]


def calculate_mutation_rate(possible_variants_vds, genome_vds):
    """
    Calculate mutation rate from all possible variants vds and observed variants vds

    :param VariantDataset possible_variants_vds: synthetic VDS
    :param VariantDataset genome_vds: gnomAD WGS VDS
    :return: keytable with mutation rates as `mutation_rate`
    :rtype: KeyTable
    """
    criteria = None  # TODO: (join with annotation vds and) build criteria

    all_possible_kt = count_variants(possible_variants_vds, criteria=criteria)
    observed_kt = count_variants(genome_vds, criteria=criteria)

    kt = (all_possible_kt.rename({'variant_count': 'possible_variants'})
          .join(observed_kt, how='outer')
          .annotate('proportion_variant = variant_count/possible_variants')
          .annotate('mutation_rate = runif(1e-9, 1e-7)'))  # TODO: currently highly accurate

    return kt.filter('context.length == 3 && ref.length == 1 && alt.length == 1 && !("N" ~ context)')


def process_consequences(vds, vep_root='va.vep'):
    """
    Adds most_severe_consequence (worst consequence for a transcript) into [vep_root].transcript_consequences,
    and worst_csq and worst_csq_suffix (worst consequence across transcripts) into [vep_root]

    :param VariantDataset vds: Input VDS
    :param str vep_root: Root for vep annotation (probably va.vep)
    :return: VDS with better formatted consequences
    :rtype: VariantDataset
    """
    vds = (vds.annotate_global_py('global.csqs', CSQ_ORDER, TArray(TString()))
           .annotate_variants_expr(
        '%(vep)s.transcript_consequences = '
        '   %(vep)s.transcript_consequences.map(csq => '
        '   let worst_csq = global.csqs.find(c => csq.consequence_terms.toSet().contains(c)) in'
        # '   let worst_csq_suffix = if (csq.filter(x => x.lof == "HC").length > 0)'
        # '       worst_csq + "-HC" '
        # '   else '
        # '       if (csq.filter(x => x.lof == "LC").length > 0)'
        # '           worst_csq + "-LC" '
        # '       else '
        # '           if (csq.filter(x => x.polyphen_prediction == "probably_damaging").length > 0)'
        # '               worst_csq + "-probably_damaging"'
        # '           else'
        # '               if (csq.filter(x => x.polyphen_prediction == "possibly_damaging").length > 0)'
        # '                   worst_csq + "-possibly_damaging"'
        # '               else'
        # '                   worst_csq in'
        '   merge(csq, {most_severe_consequence: worst_csq'
        # ', most_severe_consequence_suffix: worst_csq_suffix'
        '})'
        ')' % {'vep': vep_root}
    ).annotate_variants_expr(
        '%(vep)s.worst_csq = global.csqs.find(c => %(vep)s.transcript_consequences.map(x => x.most_severe_consequence).toSet().contains(c)),'
        '%(vep)s.worst_csq_suffix = '
        'let csq = global.csqs.find(c => %(vep)s.transcript_consequences.map(x => x.most_severe_consequence).toSet().contains(c)) in '
        'if (%(vep)s.transcript_consequences.filter(x => x.lof == "HC").length > 0)'
        '   csq + "-HC" '
        'else '
        '   if (%(vep)s.transcript_consequences.filter(x => x.lof == "LC").length > 0)'
        '       csq + "-LC" '
        '   else '
        '       if (%(vep)s.transcript_consequences.filter(x => x.polyphen_prediction == "probably_damaging").length > 0)'
        '           csq + "-probably_damaging"'
        '       else'
        '           if (%(vep)s.transcript_consequences.filter(x => x.polyphen_prediction == "possibly_damaging").length > 0)'
        '               csq + "-possibly_damaging"'
        '           else'
        '               csq' % {'vep': vep_root}
    ))
    return vds


def calibrate_model(exome_vds, all_possible_vds, mutation_kt):
    exome_vds = process_consequences(exome_vds)
    all_possible_vds = process_consequences(all_possible_vds)
    exome_kt = count_variants(exome_vds,
                              additional_groupings=['annotation = `va.vep.transcript_consequences.most_severe_consequence`',
                                                    'transcript = `va.vep.transcript_consequences.feature`'],  # va gets flattened, so this a little awkward
                              explode='va.vep.transcript_consequences')
    all_possible_kt = count_variants(all_possible_vds,
                                     additional_groupings=['annotation = `va.vep.transcript_consequences.most_severe_consequence`',
                                                           'transcript = `va.vep.transcript_consequences.feature`'],  # va gets flattened, so this a little awkward
                                     explode='va.vep.transcript_consequences')

    full_kt = all_possible_kt.rename({'variant_count': 'possible_variants'}).join(exome_kt, how='outer')
    synonymous_kt = full_kt.filter('annotation == "synonymous_variant"')

    # TODO: run regression on observed to expected and get out beta value
    # return full_kt.annotate('expected_variants = %s*possible_variants' % beta)


def get_proportion_observed(exome_vds, all_possible_vds):
    exome_vds = process_consequences(exome_vds)
    all_possible_vds = process_consequences(all_possible_vds)
    exome_kt = count_variants(exome_vds,
                              additional_groupings='annotation = `va.vep.transcript_consequences.most_severe_consequence`',  # va gets flattened, so this a little awkward
                              explode='va.vep.transcript_consequences')
    all_possible_kt = count_variants(all_possible_vds,
                                     additional_groupings='annotation = `va.vep.transcript_consequences.most_severe_consequence`',  # va gets flattened, so this a little awkward
                                     explode='va.vep.transcript_consequences')

    full_kt = all_possible_kt.rename({'variant_count': 'possible_variants'}).join(exome_kt, how='outer')
    return full_kt.annotate('proportion_observed = variant_count/possible_variants')


def main(args):
    if args.regenerate_fasta_vds:
        import_fasta(fasta_path, context_vds_path)

    context_vds = hc.read(context_vds_path).filter_variants_intervals(Interval.parse('22')).split_multi()

    if args.recalculate_mutation_rate:
        genome_vds = hc.read(final_genome_vds).filter_variants_intervals(Interval.parse('22')).split_multi().annotate_variants_vds(context_vds, code='va.context = vds.context')

        mutation_kt = calculate_mutation_rate(context_vds, genome_vds)
        mutation_kt.write(mutation_rate_kt_path)


    full_kt = None
    if args.calibrate_model:
        mutation_kt = load_mutation_rate() if args.use_old_mu else hc.read_keytable(mutation_rate_kt_path)
        exome_vds = hc.read(final_exome_vds).filter_variants_intervals(Interval.parse('22')).split_multi().annotate_variants_vds(context_vds, code='va.context = vds.context')

        proportion_observed = get_proportion_observed(exome_vds, context_vds).to_pandas().sort('proportion_observed', ascending=False)
        print(proportion_observed)
        proportion_observed.to_csv('proportion_observed.tsv', sep='\t')

        # full_kt = calibrate_model(exome_vds, context_vds, mutation_kt)
        # full_kt.write()

    if full_kt is not None:
        full_kt = hc.read_keytable('')




if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--regenerate_fasta_vds', help='Re-generate FASTA VDS', action='store_true')
    parser.add_argument('--recalculate_mutation_rate', help='Re-calculate mutation rate', action='store_true')
    parser.add_argument('--use_old_mu', help='Use old mutation rate table', action='store_true')
    parser.add_argument('--calibrate_model', help='Re-calibrate model against synonymous variants', action='store_true')
    args = parser.parse_args()
    main(args)
