"""Stand-in origin for 'MySQL CDC to Delta Lake' (MySQLBinaryLog_01): yields change records shaped like the MySQL binary-log origin's
(/Table, /Type, /Data, /OldData; header `sdc.operation.type` 1 insert, 3 update, 2 delete). The shape is assumed, not taken from a real binlog."""
from streamsets_to_dagster.runtime import Record


def produce(sdc):
    yield [
        Record({"Table": "orders", "Type": "INSERT", "Data": {"id": 1, "status": "new"}}, {"sdc.operation.type": "1"}),
        Record({"Table": "orders", "Type": "INSERT", "Data": {"id": 2, "status": "new"}}, {"sdc.operation.type": "1"}),
    ]
    yield [
        Record({"Table": "orders", "Type": "UPDATE", "Data": {"id": 1, "status": "paid"}, "OldData": {"id": 1, "status": "new"}}, {"sdc.operation.type": "3"}),
        Record({"Table": "orders", "Type": "DELETE", "OldData": {"id": 2, "status": "new"}}, {"sdc.operation.type": "2"}),
    ]
