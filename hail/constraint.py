#!/usr/bin/env python

import argparse
from utils import *
import statsmodels.formula.api as smf
import pandas as pd
from scipy import stats

hc = hail.HailContext(log="/hail.log")

# Temporary
import hail
from hail import *
from hail.expr import *
from hail.representation import *

final_exome_vds = 'gs://gnomad-public/release-170228/gnomad.exomes.r2.0.1.sites.vds'
final_genome_vds = 'gs://gnomad-public/release-170228/gnomad.genomes.r2.0.1.sites.vds'

fasta_path = "gs://gnomad-resources/Homo_sapiens_assembly19.fasta"
gerp_annotations_path = 'gs://annotationdb/cadd/cadd.kt'  # Gerp is in here as gerpS
raw_context_vds_path = "gs://gnomad-resources/Homo_sapiens_assembly19.fasta.snps_only.split.vep.vds"  # not actually split
mutation_rate_table_path = 'gs://gnomad-resources/fordist_1KG_mutation_rate_table.txt'
genome_coverage_kt_path = 'gs://gnomad-resources/genome_coverage.kt'
exome_coverage_kt_path = 'gs://gnomad-resources/exome_coverage.kt'

# Processed datasets
context_vds_path = 'gs://gnomad-resources/context_processed.vds'
genome_vds_path = 'gs://gnomad-resources/genome_processed.vds'
exome_vds_path = 'gs://gnomad-resources/exome_processed.vds'
mutation_rate_kt_path = 'gs://gnomad-resources/mutation_rate.kt'
synonymous_kt_path = 'gs://gnomad-resources/syn.kt'
full_kt_path = 'gs://gnomad-resources/constraint.kt'

CONTIG_GROUPS = ('1', '2', '3', '4', '5', '6', '7', '8-9', '10-11', '12-13', '14-16', '17-18', '19-20', '21', '22', 'X', 'Y')
# should have been: ('1', '2', '3', '4', '5', '6', '7', '8-9', '10-11', '12-13', '14-16', '17-19', '20-22', 'X', 'Y')
a_based_annotations = ['va.info.AC', 'va.info.AC_raw']


def import_fasta_and_vep(input_fasta_path, output_vds_path, overwrite=False):
    """
    Imports FASTA file with context and VEPs it. Only works with SNPs so far.
    WARNING: Very slow and annoying. Writes intermediate paths for now since VEPping the whole genome is hard.
    Some paths are hard-coded in here. Remove if needed down the line.

    :param str input_fasta_path: Input FASTA file
    :param str output_vds_path: Path to final output VDS
    :param bool overwrite: Whether to overwrite VDSes
    :return: None
    """
    input_vds_path = "gs://gnomad-resources/Homo_sapiens_assembly19.fasta.snps_only.vds"

    vds = hc.import_fasta(input_fasta_path,
                          filter_Ns=True, flanking_context=3, create_snv_alleles=True, create_deletion_size=0,
                          create_insertion_size=0, line_limit=2000)
    vds.split_multi().write(input_vds_path, overwrite=overwrite)

    for i in CONTIG_GROUPS:
        vds_name = "gs://gnomad-resources/chr_split/Homo_sapiens_assembly19.fasta.snps_only.split.%s.vds" % i
        print "Reading, splitting, and repartitioning contigs: %s..." % i
        hc.read(input_vds_path).filter_intervals(Interval.parse(i)).repartition(10000).write(vds_name,
                                                                                             overwrite=overwrite)
        print "Done! VEPping contigs: %s..." % i
        vds = hc.read(vds_name)
        vds = vds.vep(vep_config)
        vds.write(vds_name.replace('.vds', '.vep.vds'), overwrite=overwrite)

    print "Done! Unioning..."
    vds = hc.read(
        ["gs://gnomad-resources/chr_split/Homo_sapiens_assembly19.fasta.snps_only.split.%s.vep.vds" % i for i in
         CONTIG_GROUPS])
    vds.repartition(40000, shuffle=False).write(output_vds_path, overwrite=overwrite)


def load_mutation_rate():
    """
    Read old version of mutation rate table

    :return: Mutation rate keytable
    :rtype: KeyTable
    """
    kt = hc.import_table(mutation_rate_table_path, impute=True, delimiter=' ')
    return (kt.rename({'from': 'context', 'mu_snp': 'mutation_rate'})
            .annotate(['ref = context[1]', 'alt = to[1]']).key_by(['context', 'ref', 'alt']))


def reverse_complement(seq):
    complement = {'A': 'T', 'C': 'G', 'G': 'C', 'T': 'A'}
    bases = list(seq)
    bases = [complement[base] for base in bases]
    return ''.join(bases[::-1])


def rev_comp(ref, alt, context):
    if ref in ('G', 'T'):
        ref, alt, context = reverse_complement(ref), reverse_complement(alt), reverse_complement(context)
    return pd.Series({'new_ref': ref, 'new_alt': alt, 'new_context': context})


def variant_type(ref, alt, context):  # new_ref is only A and C
    if ref == 'C' and alt == 'T':
        return 'CpG' if context[2] == 'G' else 'transition'
    elif ref == 'A' and alt == 'G':
        return 'transition'
    elif ref == 'C':
        return 'CpG transversion' if context[2] == 'G' else 'transversion'
    return 'transversion'


def count_variants(vds, criteria=None, additional_groupings=None, trimer=False, explode=None, collapse_contexts=True):
    """
    Counts variants in VDS by context, ref, alt, and any other groupings provided

    :param VariantDataset vds: Input VDS
    :param str criteria: Any filtering criteria (e.g. non-coding, non-conserved), to be passed to filter_variants_expr
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

    grouping = ['context = `va.context`', 'ref = `va.ref`',
                'alt = `va.alt`']  # va is flattened, so this is a little awkward, and now ref and alt are in va.
    if additional_groupings is not None:
        if type(additional_groupings) == str:
            grouping.append(additional_groupings)
        else:
            grouping.extend(additional_groupings)

    kt = vds.variants_keytable().flatten()

    if collapse_contexts:
        kt = collapse_strand(kt)

    if explode:
        kt = kt.explode(explode).flatten()

    return kt.aggregate_by_key(grouping, 'variant_count = v.count()')


def calculate_mutation_rate(possible_variants_vds, genome_vds, criteria=None, trimer=False):
    """
    Calculate mutation rate from all possible variants vds and observed variants vds
    Currently actually calculating more like "expected_proportion_variants"

    :param VariantDataset possible_variants_vds: synthetic VDS
    :param VariantDataset genome_vds: gnomAD WGS VDS
    :param bool trimer: whether to use trimer context (default heptamer)
    :return: keytable with mutation rates as `mutation_rate`
    :rtype: KeyTable
    """

    # TODO: add methylation data
    all_possible_kt = count_variants(possible_variants_vds, criteria=criteria, trimer=trimer)
    observed_kt = count_variants(genome_vds, criteria=criteria, trimer=trimer)

    kt = (all_possible_kt.rename({'variant_count': 'possible_variants'})
          .join(observed_kt, how='outer')
          .annotate('mutation_rate = variant_count/possible_variants'))

    return kt.filter('ref.length == 1 && alt.length == 1 && !("N" ~ context)')


def process_consequences(vds, vep_root='va.vep'):
    """
    Adds most_severe_consequence (worst consequence for a transcript) into [vep_root].transcript_consequences,
    and worst_csq and worst_csq_suffix (worst consequence across transcripts) into [vep_root]

    :param VariantDataset vds: Input VDS
    :param str vep_root: Root for vep annotation (probably va.vep)
    :return: VDS with better formatted consequences
    :rtype: VariantDataset
    """
    if vep_root + '.worst_csq' in flatten_struct(vds.variant_schema, root='va'):
        vds = (vds.annotate_variants_expr('%(vep)s.transcript_consequences = '
                                          ' %(vep)s.transcript_consequences.map('
                                          '     csq => drop(csq, most_severe_consequence)'
                                          ')' % {'vep': vep_root}))
    vds = (vds.annotate_global('global.csqs', CSQ_ORDER, TArray(TString()))
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


def filter_vep_to_canonical_transcripts(vds, vep_root='va.vep'):
    return vds.annotate_variants_expr(
        '%(vep)s.transcript_consequences = '
        '   %(vep)s.transcript_consequences.filter(csq => csq.canonical == 1)' % {'vep': vep_root})


def collapse_counts_by_transcript(kt, weighted=False, additional_groupings=None):
    """

    From context, ref, alt, transcript groupings, group by transcript and returns counts.
    Can optionally weight by mutation_rate in order to generate "expected counts"

    :param KeyTable kt: key table to aggregate over
    :param bool weighted: whether variant count should be weighted by mutation_rate
    :param str or list of str additional_groupings: Additional metrics to group by (e.g. annotation)
    :return: key table grouped by only transcript
    :rtype: KeyTable
    """
    # TODO: coverage correction
    if weighted:
        kt = kt.annotate('expected_variant_count = mutation_rate * variant_count')
        aggregation_expression = 'expected_variant_count = expected_variant_count.sum()'
    else:
        aggregation_expression = 'variant_count = variant_count.sum()'

    grouping = ['transcript = transcript']
    if additional_groupings is not None:
        if type(additional_groupings) == str:
            grouping.append(additional_groupings)
        else:
            grouping.extend(additional_groupings)

    return kt.aggregate_by_key(grouping, aggregation_expression)


def build_synonymous_model(syn_kt):
    """
    Calibrates mutational model to synonymous variants in gnomAD exomes

    :param KeyTable syn_kt: Synonymous keytable
    :return:
    """
    syn_pd = syn_kt.to_pandas()
    lm = smf.ols(formula='variant_count ~ expected_variant_count', data=syn_pd).fit()
    slope = lm.params['expected_variant_count']
    intercept = lm.params['Intercept']
    return slope, intercept


def build_synonymous_model_maps(syn_kt):
    """
    Calibrates mutational model to synonymous variants in gnomAD exomes

    :param KeyTable syn_kt: Synonymous keytable
    :return:
    """
    syn_pd = syn_kt.to_pandas()
    lm = smf.ols(formula='variant_count ~ expected_variant_count', data=syn_pd).fit()
    slope = lm.params['expected_variant_count']
    intercept = lm.params['Intercept']
    return slope, intercept


def apply_model(vds, all_possible_vds, mutation_kt, syn_kt, canonical=False):
    """
    :allthethings:

    :param VariantDataset vds:
    :param VariantDataset all_possible_vds:
    :param KeyTable mutation_kt:
    :param KeyTable syn_kt:
    :param bool canonical:
    :return: Final KeyTable
    :rtype KeyTable
    """
    slope, intercept = build_synonymous_model(syn_kt)
    full_kt = get_observed_expected_kt(vds, all_possible_vds, mutation_kt, canonical=canonical,
                                       additional_groupings='annotation = annotation')
    full_kt = full_kt.annotate('expected_variant_count_adj = %s * expected_variant_count + %s' % (slope, intercept))
    return full_kt


def get_observed_expected_kt(vds, all_possible_vds, mutation_kt, canonical=False, criteria=None,
                             additional_groupings=None):
    """
    Get a set of observed and expected counts based on some criteria (and optionally for only canonical transcripts)

    :param VariantDataset vds: VDS to generate observed counts
    :param VariantDataset all_possible_vds: VDS with all possible variants
    :param KeyTable mutation_kt: Mutation rate keytable
    :param bool canonical: Whether to only use canonical transcripts
    :param str criteria: Subset of data to use (e.g. only synonymous, to create model)
    :return: KeyTable with observed (`variant_count`) and expected (`expected_variant_count`)
    :rtype KeyTable
    """
    if canonical:
        vds = filter_vep_to_canonical_transcripts(vds)
        all_possible_vds = filter_vep_to_canonical_transcripts(all_possible_vds)
    vds = process_consequences(vds)
    all_possible_vds = process_consequences(all_possible_vds)

    kt = count_variants(vds,
                        additional_groupings=['annotation = `va.vep.transcript_consequences.most_severe_consequence`',
                                              'transcript = `va.vep.transcript_consequences.transcript_id`'],
                        # va gets flattened, so this a little awkward
                        explode='va.vep.transcript_consequences', trimer=True)

    all_possible_kt = count_variants(all_possible_vds,
                                     additional_groupings=[
                                         'annotation = `va.vep.transcript_consequences.most_severe_consequence`',
                                         'transcript = `va.vep.transcript_consequences.transcript_id`'],
                                     # va gets flattened, so this a little awkward
                                     explode='va.vep.transcript_consequences', trimer=True)
    if criteria:
        kt = kt.filter(criteria)
        all_possible_kt = all_possible_kt.filter(criteria)

    collapsed_kt = collapse_counts_by_transcript(kt, additional_groupings=additional_groupings)
    all_possible_kt = all_possible_kt.key_by(['context', 'ref', 'alt']).join(
        mutation_kt.select(['context', 'ref', 'alt', 'mutation_rate']), how='outer')
    collapsed_all_possible_kt = collapse_counts_by_transcript(all_possible_kt, weighted=True,
                                                              additional_groupings=additional_groupings)

    return collapsed_kt.join(collapsed_all_possible_kt)


def get_proportion_observed(exome_vds, all_possible_vds, trimer=False):
    """
    Intermediate function to get proportion observed by context, ref, alt

    :param VariantDataset exome_vds: gnomAD exome VDS
    :param VariantDataset all_possible_vds: VDS with all possible variants
    :return: Key Table with context, ref, alt, proportion observed
    :rtype KeyTable
    """
    exome_kt = count_variants(exome_vds,
                              additional_groupings='annotation = `va.vep.transcript_consequences.most_severe_consequence`',
                              # va gets flattened, so this a little awkward
                              explode='va.vep.transcript_consequences', trimer=trimer)
    all_possible_kt = count_variants(all_possible_vds,
                                     additional_groupings='annotation = `va.vep.transcript_consequences.most_severe_consequence`',
                                     # va gets flattened, so this a little awkward
                                     explode='va.vep.transcript_consequences', trimer=trimer)

    full_kt = all_possible_kt.rename({'variant_count': 'possible_variants'}).join(exome_kt, how='outer')
    return full_kt.annotate('proportion_observed = variant_count/possible_variants')


def run_sanity_checks(vds, exome=True, csq_queries=False, return_data=False):
    """

    :param VariantDataset vds: Input VDS
    :param bool exome: Run and return exome queries
    :param bool csq_queries: Run and return consequence queries
    :return: whether VDS was split, queries, full sanity results, [exome sanity results], [csq results]
    """
    sanity_queries = ['variants.count()',
                      'variants.filter(v => isMissing(va.vep)).count()',
                      'variants.filter(v => isMissing(va.vep.transcript_consequences)).count()',
                      'variants.filter(v => isMissing(va.gerp)).count()']

    additional_queries = ['variants.map(v => va.vep.worst_csq).counter()',
                          'variants.map(v => va.vep.worst_csq_suffix).counter()']

    # [x.replace('variants.', 'variants.filter(v => ).') for x in sanity_queries]
    # should probably annotate_variants_intervals and filter directly in query

    full_sanity_results = vds.query_variants(sanity_queries)

    if return_data:
        results = [vds.was_split(), sanity_queries, full_sanity_results]
    else:
        print 'Was split: %s' % vds.was_split()
        print "All data:\n%s" % zip(sanity_queries, full_sanity_results)

    if exome:
        exome_intervals = KeyTable.import_interval_list(exome_calling_intervals)
        exome_intervals_sanity_results = (vds.filter_variants_table(exome_intervals)
                                          .query_variants(sanity_queries))
        if return_data:
            results.append(exome_intervals_sanity_results)
        else:
            print "Exome Intervals:\n%s" % zip(sanity_queries, exome_intervals_sanity_results)

    if csq_queries:
        csq_query_results = vds.query_variants(additional_queries)
        if return_data:
            results.append(csq_query_results)
        else:
            print csq_query_results

    if return_data:
        return results


def collapse_strand(kt):
    """
    :param KeyTable kt: Keytable with context, ref, alt to collapse strands
    :return: Keytable with collapsed strands (puts ref and alt into va.ref and va.alt)
    :rtype KeyTable
    """

    def flip_text(root):
        return ('let base = %s in '
                'if (base == "A") "T" else '
                'if (base == "T") "A" else '
                'if (base == "C") "G" else '
                'if (base == "G") "C" else base' % root
        )

    return kt.annotate(['va.ref = if (v.ref == "T" || v.ref == "G") %s else v.ref' % flip_text('v.ref'),
                        'va.alt = if (v.ref == "T" || v.ref == "G") %s else v.alt' % flip_text('v.alt'),
                        'va.context = if (v.ref == "T" || v.ref == "G") %s + %s + %s '
                        'else va.context' % (
                            flip_text('va.context[2]'), flip_text('va.context[1]'), flip_text('va.context[0]'))])


def maps(vds, mutation_kt, additional_groupings=None, trimer=True):
    """

    :param VariantDataset vds: VDS
    :return:
    """

    if trimer: vds = vds.annotate_variants_expr('va.context = va.context[2:5]')

    kt = vds.variants_keytable().annotate('va.singleton = (va.info.AC == 1).toLong')

    syn_kt = (kt.filter('va.vep.worst_csq == "synonymous_variant"')
              .aggregate_by_key(['context = va.context', 'ref = v.ref', 'alt = v.alt'],  # check if we need to flip here
                                'proportion_singleton = va.singleton.sum()/v.count()')
              .join(mutation_kt))

    syn_pd = syn_kt.to_pandas()
    lm = smf.ols(formula='proportion_singleton ~ mutation_rate', data=syn_pd).fit()
    slope = lm.params['mutation_rate']
    intercept = lm.params['Intercept']

    grouping = ['va.vep.worst_csq']
    if additional_groupings is not None:
        if type(additional_groupings) == str:
            grouping.append(additional_groupings)
        else:
            grouping.extend(additional_groupings)

    kt = (kt
          .annotate('expected_ps = %s * mutation_rate + %s' % (slope, intercept))
          .aggregate_by_key(grouping,
                            ['num_singletons = va.singleton.sum()',
                             'num_variants = v.count()',
                             'total_expected_ps = expected_ps.sum()'])
          .annotate(['raw_ps = num_singletons/num_variants',
                     'maps = (num_singletons - total_expected_ps)/num_variants'])
          .annotate(['ps_sem = sqrt(raw_ps*(1-raw_ps)/num_variants)']))


def main(args):
    if args.generate_fasta_vds:
        import_fasta_and_vep(fasta_path, context_vds_path, args.overwrite)

    if args.pre_process_data:
        # Pre-process context, genome, and exome data
        exome_coverage_kt = hc.read_table(exome_coverage_kt_path)
        genome_coverage_kt = hc.read_table(genome_coverage_kt_path)

        gerp_kt = hc.read_table(gerp_annotations_path)
        context_vds = process_consequences(hc.read(raw_context_vds_path)
                                           .split_multi()
                                           .annotate_variants_expr(index_into_arrays(None, vep_root='va.vep'))
                                           .annotate_variants_table(exome_coverage_kt, expr='va.coverage.exome = table')
                                           .annotate_variants_table(genome_coverage_kt,
                                                                    expr='va.coverage.genome = table')
                                           .annotate_variants_table(gerp_kt, expr='va.gerp = table.GerpS')
        )
        context_vds.write(context_vds_path, overwrite=args.overwrite)
        context_vds = hc.read(context_vds_path)
        genome_vds = (hc.read(final_genome_vds).split_multi()
                      .annotate_variants_vds(context_vds,
                                             code='va.context = vds.context, va.gerp = vds.gerp, va.vep = vds.vep, va.coverage = vds.coverage'))
        genome_vds.write(genome_vds_path, overwrite=args.overwrite)
        exome_vds = (hc.read(final_exome_vds).split_multi()
                     .annotate_variants_expr(index_into_arrays(a_based_annotations))
                     .annotate_variants_vds(context_vds,
                                            code='va.context = vds.context, va.gerp = vds.gerp, va.vep = vds.vep, va.coverage = vds.coverage'))
        exome_vds.write(exome_vds_path, overwrite=args.overwrite)

    context_vds = hc.read(context_vds_path)
    genome_vds = hc.read(genome_vds_path)
    exome_vds = hc.read(exome_vds_path)

    if args.run_sanity_checks:
        run_sanity_checks(context_vds)
        run_sanity_checks(genome_vds)
        run_sanity_checks(exome_vds, exome=False, csq_queries=True)
        proportion_observed = get_proportion_observed(exome_vds, context_vds, trimer=True).to_pandas().sort(
            'proportion_observed', ascending=False)
        print(proportion_observed)

    if args.calculate_mutation_rate:
        # TODO: Exclude LCR, SEGDUP, repetitive regions?
        # TODO: PCR-free only and remove low-coverage regions, only PASS?
        mutation_kt = calculate_mutation_rate(context_vds,
                                              genome_vds.filter_intervals(Interval.parse('1-22')),
                                              criteria='isDefined(va.gerp) && va.gerp < 0 && isMissing(va.vep.transcript_consequences)',
                                              trimer=True)
        mutation_kt = collapse_strand(mutation_kt)
        mutation_kt.repartition(1).write(mutation_rate_kt_path, overwrite=args.overwrite)
        mutation_kt.export(mutation_rate_kt_path.replace('.kt', '.txt.bgz'))

    mutation_kt = hc.read_table(mutation_rate_kt_path) if not args.use_old_mu else load_mutation_rate()

    if args.calibrate_model:
        syn_kt = get_observed_expected_kt(exome_vds, context_vds, mutation_kt, canonical=True,
                                          criteria='annotation == "synonymous_variant"')
        syn_kt.repartition(10).write(synonymous_kt_path, overwrite=args.overwrite)

    syn_kt = hc.read_table(synonymous_kt_path)

    if args.build_full_model:
        full_kt = apply_model(exome_vds, context_vds, mutation_kt, syn_kt, canonical=False)
        full_kt.repartition(10).write(full_kt_path, overwrite=args.overwrite)
        full_kt.export(full_kt.replace('.kt', '.txt.bgz'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--overwrite', help='Overwrite everything', action='store_true')
    parser.add_argument('--generate_fasta_vds', help='Generate FASTA VDS', action='store_true')
    parser.add_argument('--pre_process_data', help='Pre-process all data (context, genome, exome)', action='store_true')
    parser.add_argument('--run_sanity_checks', help='Run sanity checks on all VDSes', action='store_true')
    parser.add_argument('--calculate_mutation_rate', help='Calculate mutation rate', action='store_true')
    parser.add_argument('--calibrate_model', help='Re-calibrate model against synonymous variants', action='store_true')
    parser.add_argument('--build_full_model', help='Build full model', action='store_true')
    parser.add_argument('--use_old_mu', help='Use old mutation rate table', action='store_true')
    args = parser.parse_args()
    main(args)
