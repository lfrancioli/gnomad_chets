from variantqc import *
import time

# Inputs

bucket = 'gs://gnomad-exomes'
autosomes_intervals = '%s/intervals/autosomes.txt' % bucket
# evaluation_intervals = '%s/intervals/exome_evaluation_regions.v1.intervals' % bucket
# high_coverage_intervals = '%s/intervals/high_coverage.auto.interval_list' % bucket
meta_path = 'gs://gnomad-exomes-raw/super_meta.txt.bgz'

root = '%s/sites' % bucket

vds_path = 'gs://gnomad-exomes-raw/full/gnomad.exomes.all.vds'

rf_path = '%s/variantqc/gnomad.exomes.variantqc.vds' % bucket
vep_config = "/vep/vep-gcloud.properties"

# Outputs
out_vds_prefix = "%s/gnomad.exomes.sites" % root
out_internal_vcf_prefix = "%s/gnomad.exomes.sites.internal" % root
out_external_vcf_prefix = "%s/gnomad.exomes.sites" % root

#Config
pops = ['AFR', 'AMR', 'ASJ', 'EAS', 'FIN', 'NFE', 'OTH', 'SAS']
rf_snv_cutoff = 0.1
rf_indel_cutoff = 0.2

#Actions
run_all = False
run_auto = False
run_x = False
run_y = False
run_pre = False
run_post = False
write = True
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
        .write(out_vds_prefix + ".pre.vds")
    )

if postprocess_autosomes:
    rf_vds = hc.read(rf_path)
    post_process_vds(hc, out_vds_prefix + ".pre.autosomes.vds", rf_vds, 'va.rf', 'va.train', 'va.label', rf_snv_cutoff, rf_indel_cutoff, vep_config).write(out_vds_prefix + ".autosomes.vds")

if write_autosomes:
    vds = hc.read(out_vds_prefix + ".vds").filter_variants_intervals(autosomes_intervals)
    write_vcfs(vds, '', out_internal_vcf_prefix, out_external_vcf_prefix, append_to_header=additional_vcf_header)

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
    post_process_vds(hc, out_vds_prefix + ".pre.X.vds", rf_vds, 'va.rf', 'va.train', 'va.label', rf_snv_cutoff, rf_indel_cutoff, vep_config).write(out_vds_prefix + ".X.vds")

if write_X:
    write_vcfs(hc.read(out_vds_prefix + ".X.vds"), "X", out_internal_vcf_prefix, out_external_vcf_prefix, append_to_header=additional_vcf_header)

if preprocess_Y:
    (
        create_sites_vds_annotations_Y(
            preprocess_vds(vds_path),
            pops,
            dbsnp_path=dbsnp_vcf)
        .write(out_vds_prefix + ".pre.Y.vds")
    )

if postprocess_Y:
    rf_vds = hc.read(rf_path)
    post_process_vds(hc, out_vds_prefix + ".pre.Y.vds", rf_vds, 'va.rf', 'va.train', 'va.label', rf_snv_cutoff, rf_indel_cutoff, vep_config).write(out_vds_prefix + ".Y.vds")

if write_Y:
    write_vcfs(hc.read(out_vds_prefix + ".Y.vds"), "Y", out_internal_vcf_prefix, out_external_vcf_prefix, append_to_header=additional_vcf_header)

send_message(channel='#joint_calling', message='Exomes are done processing!')

# zcat gnomad.exomes.sites.autosomes.vcf.gz | head -250 | grep "^##" > header
# zcat gnomad.exomes.sites.X.vcf.gz | head -250 | grep "^##" | while read i; do grep -F "$i" header; if [[ $? != 0 ]]; then echo $i >> header; fi; done
# Optional: nano header to move CSQ, contigs, and reference below X specific annotations
# cat header <(zcat gnomad.exomes.sites.autosomes.vcf.gz | grep -v "^##") <(zcat gnomad.exomes.sites.X.vcf.gz | grep -v "^##") <(zcat gnomad.exomes.sites.Y.vcf.gz | grep -v "^##") | bgzip -c > gnomad.exomes.sites.vcf.gz