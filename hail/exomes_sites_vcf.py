from variantqc import *
from hail import *
import time

# Inputs

bucket = 'gs://gnomad-exomes'
autosomes_intervals = '%s/intervals/autosomes.txt' % bucket
# evaluation_intervals = '%s/intervals/exome_evaluation_regions.v1.intervals' % bucket
# high_coverage_intervals = '%s/intervals/high_coverage.auto.interval_list' % bucket
meta_path = 'gs://gnomad-exomes-raw/super_meta.txt.bgz'
date_time = time.strftime("%Y-%m-%d_%H:%M")

root = '%s/sites' % bucket

vds_path = 'gs://gnomad-exomes-raw/full/gnomad.exomes.all.vds'

rf_path = '%s/variantqc/gnomad.exomes.rf.vds' % bucket

# Outputs
out_vds_prefix = "%s/internal/gnomad.exomes.sites" % root
out_internal_vcf_prefix = "%s/internal/gnomad.exomes.sites.internal" % root
out_external_vcf_prefix = "%s/vcf/gnomad.exomes.sites" % root

#Config
pops = ['AFR', 'AMR', 'ASJ', 'EAS', 'FIN', 'NFE', 'OTH', 'SAS']
RF_SNV_CUTOFF = 0.1
RF_INDEL_CUTOFF = 0.2
send_to_slack = True

#Actions
run_all = False
run_auto = False
run_x = False
run_y = False
run_pre = False
run_post = False
write = False
write_vds = run_all or False
run_pre_calculate_metrics = True
preprocess_autosomes = run_all or run_auto or run_pre or False
postprocess_autosomes = run_all or run_auto or run_post or False
write_autosomes = run_all or run_auto or write or False
preprocess_X = run_all or run_x or run_pre or False
postprocess_X = run_all or run_x or run_post or False
write_X = run_all or run_x or write or False
preprocess_Y = run_all or run_y or run_pre or False
postprocess_Y = run_all or run_y or run_post or False
write_Y = run_all or run_y or write or False

hc = HailContext()


def preprocess_vds(vds_path):
    print("Preprocessing %s\n" % vds_path)
    vqsr_vds = hc.read('gs://gnomad-exomes/variantqc/gnomad.exomes.vqsr.unsplit.vds')
    annotations = ['culprit', 'POSITIVE_TRAIN_SITE', 'NEGATIVE_TRAIN_SITE', 'VQSLOD']
    return (hc.read(vds_path)
            .annotate_global_py('global.pops', map(lambda x: x.lower(), pops), TArray(TString()))
            .annotate_samples_table(meta_path, 'sample', root='sa.meta', config=hail.TextTableConfig(impute=True))
            .filter_samples_expr('sa.meta.drop_status == "keep"')
            .annotate_samples_expr(['sa.meta.project_description = sa.meta.description'])  # Could be cleaner
            .annotate_variants_intervals(decoy_path, 'va.decoy')
            .annotate_variants_intervals(lcr_path, 'va.lcr')
            .annotate_variants_vds(vqsr_vds, code=', '.join(['va.info.%s = vds.info.%s' % (a, a) for a in annotations]))
    )


if preprocess_autosomes:
    (
        create_sites_vds_annotations(
            preprocess_vds(vds_path),
            pops,
            dbsnp_path=dbsnp_vcf)
        .write(out_vds_prefix + ".pre.autosomes.vds")
    )

if postprocess_autosomes:
    rf_vds = hc.read(rf_path)
    post_process_vds(hc, out_vds_prefix + ".pre.autosomes.vds",
                     rf_vds,
                     RF_SNV_CUTOFF, RF_INDEL_CUTOFF,
                     'va.rf').write(out_vds_prefix + ".autosomes.vds", overwrite=True)

    vds = hc.read(out_vds_prefix + ".autosomes.vds")
    sanity_check = run_sanity_checks(vds, pops, return_string=send_to_slack)
    if send_to_slack: send_snippet('#joint_calling', sanity_check, 'autosome_sanity_%s.txt' % date_time)

if write_autosomes:
    vds = hc.read(out_vds_prefix + ".autosomes.vds").filter_variants_intervals(autosomes_intervals).filter_variants_intervals(exome_calling_intervals)
    write_vcfs(vds, '', out_internal_vcf_prefix, out_external_vcf_prefix, RF_SNV_CUTOFF, RF_INDEL_CUTOFF, append_to_header=additional_vcf_header)

if write_vds:
    vds = hc.read(out_vds_prefix + ".autosomes.vds").filter_variants_intervals(autosomes_intervals).filter_variants_intervals(exome_calling_intervals)
    write_public_vds(hc, vds, out_vds_prefix + ".final.autosomes.vds", out_external_vcf_prefix.replace('vcf', 'vds') + ".release.autosomes.vds")

if preprocess_X:
    (
        create_sites_vds_annotations_X(
            preprocess_vds(vds_path),
            pops,
            dbsnp_path=dbsnp_vcf)
        .write(out_vds_prefix + ".pre.X.vds")
    )

if postprocess_X:
    rf_vds = hc.read(rf_path)
    post_process_vds(hc, out_vds_prefix + ".pre.X.vds",
                     rf_vds,
                     RF_SNV_CUTOFF, RF_INDEL_CUTOFF,
                     'va.rf').write(out_vds_prefix + ".X.vds", overwrite=True)
    vds = hc.read(out_vds_prefix + ".X.vds")
    sanity_check = run_sanity_checks(vds, pops, contig='X', return_string=send_to_slack)
    if send_to_slack: send_snippet('#joint_calling', sanity_check, 'x_sanity_%s.txt' % date_time)

if write_X:
    vds = hc.read(out_vds_prefix + ".X.vds").filter_variants_intervals(exome_calling_intervals)
    write_vcfs(vds, "X", out_internal_vcf_prefix, out_external_vcf_prefix, RF_SNV_CUTOFF, RF_INDEL_CUTOFF, append_to_header=additional_vcf_header)

if write_vds:
    vds = hc.read(out_vds_prefix + ".X.vds")
    write_public_vds(hc, vds, out_vds_prefix + ".final.X.vds", out_external_vcf_prefix.replace('vcf', 'vds') + ".release.X.vds")

if preprocess_Y:
    (
        create_sites_vds_annotations_Y(
            preprocess_vds(vds_path),
            pops,
            dbsnp_path=dbsnp_vcf)
        .write(out_vds_prefix + ".pre.Y.vds")
    )

if postprocess_Y:
    rf_vds = hc.read(rf_path).filter_variants_intervals(exome_calling_intervals)
    post_process_vds(hc, out_vds_prefix + ".pre.Y.vds",
                     rf_vds,
                     RF_SNV_CUTOFF, RF_INDEL_CUTOFF,
                     'va.rf').write(out_vds_prefix + ".Y.vds", overwrite=True)
    vds = hc.read(out_vds_prefix + ".Y.vds")
    sanity_check = run_sanity_checks(vds, pops, contig='Y', return_string=send_to_slack)
    if send_to_slack: send_snippet('#joint_calling', sanity_check, 'y_sanity_%s.txt' % date_time)

if write_Y:
    vds = hc.read(out_vds_prefix + ".Y.vds").filter_variants_intervals(exome_calling_intervals)
    write_vcfs(vds, "Y", out_internal_vcf_prefix, out_external_vcf_prefix, RF_SNV_CUTOFF, RF_INDEL_CUTOFF, append_to_header=additional_vcf_header)

if write_vds:
    vds = hc.read(out_vds_prefix + ".Y.vds")
    write_public_vds(hc, vds, out_vds_prefix + ".final.Y.vds", out_external_vcf_prefix.replace('vcf', 'vds') + ".release.Y.vds")

if run_pre_calculate_metrics:
    vds = hc.read(out_external_vcf_prefix.replace('vcf', 'vds') + ".release.autosomes.vds")
    pre_calculate_metrics(vds, out_external_vcf_prefix + ".metrics")

send_message(channel='@konradjk', message='Exomes are done processing!')

# zcat gnomad.exomes.sites.autosomes.vcf.bgz | head -250 | grep "^##" > header
# zcat gnomad.exomes.sites.X.vcf.bgz | head -250 | grep "^##" | while read i; do grep -F "$i" header; if [[ $? != 0 ]]; then echo $i >> header; fi; done
# Optional: nano header to move CSQ, contigs, and reference below X specific annotations
# cat header <(zcat gnomad.exomes.sites.autosomes.vcf.bgz | grep -v "^##") <(zcat gnomad.exomes.sites.X.vcf.bgz | grep -v "^#") <(zcat gnomad.exomes.sites.Y.vcf.bgz | grep -v "^#") | bgzip -c > gnomad.exomes.sites.vcf.gz