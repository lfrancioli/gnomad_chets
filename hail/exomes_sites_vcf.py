from variantqc import *
from hail import *
import time
import argparse

pops = ['AFR', 'AMR', 'ASJ', 'EAS', 'FIN', 'NFE', 'OTH']  # Adding SAS later for exomes


def preprocess_exomes_vds(vds, meta_kt, vqsr_vds, vds_pops, release=True):
    annotations = ['culprit', 'POSITIVE_TRAIN_SITE', 'NEGATIVE_TRAIN_SITE', 'VQSLOD']
    pre_vds = (vds
               .annotate_global('global.pops', map(lambda x: x.lower(), vds_pops), TArray(TString()))
               .annotate_samples_table(meta_kt, root='sa.meta')
               .annotate_samples_expr(['sa.meta.project_description = sa.meta.description'])  # Could be cleaner
               .annotate_variants_table(KeyTable.import_bed(decoy_path), root='va.decoy')
               .annotate_variants_table(KeyTable.import_interval_list(lcr_path), root='va.lcr')
               .annotate_variants_vds(vqsr_vds, code=', '.join(['va.info.%s = vds.info.%s' % (a, a) for a in annotations]))
    )
    return pre_vds.filter_samples_expr('sa.meta.drop_status == "keep"') if release else pre_vds


def preprocess_genomes_vds(vds, meta_kt, vqsr_vds, vds_pops, release=True):
    pass


def main(args):
    if args.debug: logger.setLevel(logging.DEBUG)
    hc = HailContext()

    if args.genomes:
        vds_path = full_genome_vds
        meta_file = genomes_meta
        RF_SNV_CUTOFF = None
        RF_INDEL_CUTOFF = None
        preprocess_vds = preprocess_genomes_vds
        vqsr_vds = None
        rf_path = ''
        running = 'genomes'
    else:
        vds_path = full_exome_vds
        pops.append('SAS')
        meta_file = exomes_meta
        RF_SNV_CUTOFF = 0.1
        RF_INDEL_CUTOFF = 0.2
        preprocess_vds = preprocess_exomes_vds
        vqsr_vds = hc.read(vqsr_vds_path)
        rf_path = 'gs://gnomad-exomes/variantqc/170620_new/gnomad.exomes.rf.vds'
        running = 'exomes'

    if not (args.skip_preprocess_autosomes or args.skip_preprocess_X or args.skip_preprocess_Y):
        vds = hc.read(vds_path)
        meta_kt = hc.import_table(meta_file, impute=True).key_by('sample')
        vds = preprocess_vds(vds, meta_kt, vqsr_vds, vds_pops=pops)
        if args.expr:
            vds = vds.filter_samples_expr(args.expr)
        logger.info('Found %s samples', vds.query_samples('samples.count()'))

        dbsnp_kt = (hc
                    .import_table(dbsnp_vcf, comment='#', no_header=True, types={'f0': TString(), 'f1': TInt()})
                    .annotate('locus = Locus(f0, f1)')
                    .key_by('locus')
        )
        if not args.skip_preprocess_autosomes:
            (
                create_sites_vds_annotations(vds, pops, dbsnp_kt=dbsnp_kt)
                .write(args.output + ".pre.autosomes.vds")
            )

        if not args.skip_preprocess_X:
            (
                create_sites_vds_annotations_X(vds, pops, dbsnp_kt=dbsnp_kt)
                .write(args.output + ".pre.X.vds")
            )

        if args.exomes and not args.skip_preprocess_Y:
            (
                create_sites_vds_annotations_Y(vds, pops, dbsnp_kt=dbsnp_kt)
                .write(args.output + ".pre.Y.vds")
            )

    if not args.skip_merge:
        vdses = [hc.read(args.output + ".pre.autosomes.vds"), hc.read(args.output + ".pre.X.vds")]
        if args.exomes: vdses.append(hc.read(args.output + ".pre.Y.vds"))
        vdses = merge_schemas(vdses)
        vds = vdses[0].union(vdses[1:])
        vds.write(args.output + '.pre.vds', overwrite=args.overwrite)

    if not args.skip_vep:
        (hc.read(args.output + ".pre.vds")
         .vep(config=vep_config, csq=True, root='va.info.CSQ')
         .write(args.output + ".pre.vep.vds", overwrite=args.overwrite)
         )

    if not args.skip_postprocess:
        vds = hc.read(args.output + ".pre.vep.vds")
        rf_vds = hc.read(rf_path)
        post_process_vds(vds, rf_vds, RF_SNV_CUTOFF, RF_INDEL_CUTOFF,
                         'va.rf').write(args.output + ".post.vds", overwrite=args.overwrite)

        vds = hc.read(args.output + ".post.vds")
        sanity_check = run_sites_sanity_checks(vds, pops)
        if args.slack_channel: send_snippet(args.slack_channel, sanity_check, 'sanity_%s.txt' % time.strftime("%Y-%m-%d_%H:%M"))

    if not args.skip_write:
        vds = hc.read(args.output + ".post.vds")
        if args.exomes:
            exome_intervals = KeyTable.import_interval_list(exome_calling_intervals)
            vds = vds.filter_variants_table(exome_intervals)
        write_vcfs(vds, '', args.output, False, RF_SNV_CUTOFF, RF_INDEL_CUTOFF, append_to_header=additional_vcf_header)
        write_public_vds(vds, args.output + ".vds", overwrite=args.overwrite)

    if not args.skip_pre_calculate_metrics:
        vds = hc.read(args.output + ".vds")
        fname = '{}_precalculated_metrics.txt'.format(running)
        pre_calculate_metrics(vds, fname)
        send_snippet('#gnomad_browser', open(fname).read())

    if args.slack_channel: send_message(channel=args.slack_channel, message='{} are done processing!'.format(running.capitalize()))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--exomes', help='Input VDS is exomes. One of --exomes or --genomes is required.', action='store_true')
    parser.add_argument('--genomes', help='Input VDS is genomes. One of --exomes or --genomes is required.', action='store_true')
    parser.add_argument('--skip_preprocess_autosomes', help='Skip pre-processing autosomes (assuming already done)', action='store_true')
    parser.add_argument('--skip_preprocess_X', help='Skip pre-processing X (assuming already done)', action='store_true')
    parser.add_argument('--skip_preprocess_Y', help='Skip pre-processing Y (assuming already done)', action='store_true')
    parser.add_argument('--skip_postprocess', help='Skip merge and post-process (assuming already done)', action='store_true')
    parser.add_argument('--skip_merge', help='Skip merging data (assuming already done)', action='store_true')
    parser.add_argument('--skip_vep', help='Skip VEPping data (assuming already done)', action='store_true')
    parser.add_argument('--skip_write', help='Skip writing data (assuming already done)', action='store_true')
    parser.add_argument('--skip_pre_calculate_metrics', help='Skip pre-calculating metrics (assuming already done)', action='store_true')
    parser.add_argument('--overwrite', help='Overwrite data', action='store_true')
    parser.add_argument('--expr', help='''Additional expression (e.g. "!sa.meta.remove_for_non_tcga)"''')
    parser.add_argument('--debug', help='Prints debug statements', action='store_true')
    parser.add_argument('--slack_channel', help='Slack channel to post results and notifications to.')
    parser.add_argument('--output', '-o', help='Output prefix', required=True)
    args = parser.parse_args()

    if int(args.exomes) + int(args.genomes) != 1:
        sys.exit('Error: One and only one of --exomes or --genomes must be specified')

    main(args)