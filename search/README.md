# Multi-source search registry

This directory is the user-configurable registry used by the Research search MCP.

```text
search/
├── .env                 # local values, ignored by Git
├── .env.example         # committed template
├── domains/             # domain/operation profiles that fan out to sources
├── providers/           # bundled provider implementations
│   ├── academic/        # academic entrypoint and its local sub-providers
│   ├── github_issues.py
│   ├── github_repositories.py
│   └── ...              # one executable entrypoint per provider
└── sources/
    ├── academic.yaml
    ├── github_repositories.yaml
    └── ...              # one YAML file per provider
```

The MCP discovers every `sources/*.yaml` file at startup. There is no Python-side
provider catalog. To register a source, add one YAML file whose filename matches
its `name`; no registry edit is required.

`domains/*.yaml` is an optional layer above the source registry. Each domain
declares named operations and the source set relevant to each operation. Custom
registries without a `domains/` directory remain compatible with source-level
`batch_search`. The bundled registry exposes academic, financial-market,
corporate-disclosure, software-engineering, AI-model, social-community,
video-media, and general-web profiles.

Research agents should normally call `list_search_domains`, submit all relevant
domain/operation requests through `start_domain_search`, poll
`get_search_batch`, and then page `search_results`. `list_search_sources` and
`batch_search` remain source-level compatibility and diagnostic tools.

Each source declares:

- the Python script and argument templates (`{query}` and `{limit}`);
- source-selection guidance exposed to the Research agent;
- timeout and per-source concurrency;
- required Python modules;
- required and optional environment variables;
- an optional result-shape adapter for non-generic JSON.

`script` may be absolute, relative to this directory, or use `${NAME}` values
from `.env`. Source scripts are trusted local code and execute without an OS
sandbox. Use fixed, reviewed script versions.

For a fresh checkout:

```bash
cp search/.env.example search/.env
```

The bundled provider scripts are part of this repository, so no external Skill
checkout or script-root variable is required. Fill only the credentials needed
by the sources you intend to use. To use another registry, pass
`--search-dir /path/to/search` to `doctor`, a workflow run, or `resume`.

Validate discovery without starting Hermes:

```bash
deepresearch sources list --search-dir ./search --json
deepresearch sources describe academic --search-dir ./search --json
deepresearch domains list --search-dir ./search --json
deepresearch domains describe academic --search-dir ./search --json
```
