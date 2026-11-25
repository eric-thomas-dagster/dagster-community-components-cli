# What to ask the customer for

1. **Pipeline exports.** From Data Collector: select pipelines, Export (a JSON file each, or a `.zip`). From Control Hub: Pipelines, select, Export (a `.zip`). Send pipelines in "include library definitions" or plain form; both load. The Python SDK's `export_pipelines()` also works.
2. **A note on secrets.** Exports can carry credentials inside runtime parameters (`constants`). The converter drops them, but send the archive through the customer's approved channel and tell them what it may contain.
3. **Which pipelines are continuous.** Anything with CDC, Kafka, HTTP server, directory spooling or file tail, and whether the lag matters.
4. **What starts each batch pipeline.** A Control Hub schedule, an external scheduler, manual. The export holds none of it.
5. **Which pipelines are retired or duplicated** (estates accumulate copies).
6. **Which databases** the JDBC stages point at (the converter derives SQLAlchemy URLs for MySQL, MariaDB, PostgreSQL and Redshift only).
7. **Their Data Collector or Transformer version.** Expression functions and defaults differ between versions.
8. **Any pipeline that uses scripting stages** (Groovy, Jython, JavaScript): those are rewritten by hand.
