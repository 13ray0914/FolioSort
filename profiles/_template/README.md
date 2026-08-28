# Creating another literature-review domain profile

The pipeline code is domain-independent as long as a profile preserves these top-level contracts:

- `inventory.schema.json`: `article_type`, `objectives`, `systems`, `methods`, `studied_properties`, `global_conditions`
- `evidence.schema.json`: `measurements`, `claims`, `limitations`, `citation_contexts`
- `prompts/inventory_system.txt`
- `prompts/evidence_system.txt`
- optional `review_checklist.txt`

To create a new domain:

1. Copy `profiles/peg` to `profiles/my_domain`.
2. Edit the `systems[].attributes` fields in `inventory.schema.json` for the new domain.
3. Rewrite the two prompt files with the domain ontology and extraction rules.
4. Edit `review_checklist.txt`.
5. Set `"profile": "my_domain"` in `config.json`.

No Python scripts need to be changed.
