"""Hand-written replacement for the Groovy Evaluator in 'AWS Marketplace Data' (GroovyEvaluator_01).

The Groovy script fills blank fields of a record from the last record in the batch that has a value in the pipeline's first field
(`first_field` header attribute): the AWS reports leave repeated values blank under a group's first row.
"""


def process(records, sdc):
    if not records:
        return
    first = records[0].attributes.get("first_field")
    full = None
    for rec in records:
        if first in rec.value and rec.value[first]:
            full = rec
        elif full is not None:
            for key in list(rec.value):
                if not str(rec.value.get(key) or "").strip():
                    rec.value[key] = full.value.get(key)
        sdc.output.write(rec)
