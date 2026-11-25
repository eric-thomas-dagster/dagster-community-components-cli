"""Hand-written replacements that retire the Hadoop half of 'Drift Synchronization for Hive': the data lane is written to a local file."""
import json
import os


def metadata(records, sdc):                       # HiveMetadata_01: pass the data lane through, drop the Hive metadata lane
    for rec in records:
        sdc.output.write(rec, 0)


def to_file(records, sdc):                        # HadoopFS_01
    path = os.environ["SDC_TEST_OUT"]
    with open(path, "a") as fh:
        for rec in records:
            fh.write(json.dumps(rec.value, default=str) + "\n")


def nothing(records, sdc):                        # HiveMetastore_01, HiveQuery_01
    return
