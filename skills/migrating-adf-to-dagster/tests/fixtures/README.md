# Fixtures

Copied unmodified from Microsoft's public sample repo
https://github.com/Azure/Azure-DataFactory (MIT license, see
LICENSE-Azure-DataFactory.txt), commit d470e06.

- `arm/` are template-gallery exports in ARM form (`resources[]` with
  `Microsoft.DataFactory/factories/pipelines`, some values deploy-time
  `[parameters('x')]` strings).
- `native/` is the shape a git-integrated factory writes (one JSON file per
  pipeline, trigger, dataset, linked service). `LS-AzureSQL.json` carries a
  sample connection string with a password: it is here to test that the
  converter never copies secrets into its output.

They are teaching examples, not a customer sample. Replace or add to them
with a real export before trusting any coverage figure.
