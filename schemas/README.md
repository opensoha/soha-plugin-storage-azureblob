# Soha plugin schema snapshots

The three JSON files are byte-for-byte copies from `contracts/plugins/` in Soha commit `2e6727e4bc48a4a8786765d2b6869e4394f8668e` (Task 136, U61). The release tools load them locally. The attestation schema references the manifest schema, which references the companion-pack schema.

When updating the contract pin, obtain all three files from the same verified Soha revision or published Contracts artifact. Replace the snapshots together, record the new revision here, and rerun the manifest and signed-release positive and negative checks. Do not edit the JSON files independently of the upstream contract.
