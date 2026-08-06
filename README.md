# etna-kits

The curated kit repository for [Etna](https://github.com/dwhite-sys/Etna).

## Installing a kit

```
etna install <kit_name>
etna install <kit_name>==<version>
```

For example:
```
etna install ntfy
etna install ntfy==1.0.0b1
```

## Kits

| Name | Stem | Description |
|------|------|-------------|
| Ntfy | `ntfy` | Send push notifications to a self-hosted ntfy instance |
| Web Kit | `web` | Web crawling, search, and HTTP request toolkit |
| Playwright | `playwright` | Browser automation via Chrome CDP |

## Structure

```
etna-kits/
├── manifest.json                    ← kit index fetched by the Etna CLI
└── kits/
    └── <stem>/
        └── versions/
            └── <version>/
                └── <stem>.py
```

## Contributing

This is a manually curated repository. To submit a kit, open a pull request with your kit file at `kits/<stem>/versions/<version>/<stem>.py` and an updated `manifest.json` entry pointing at the new version.
