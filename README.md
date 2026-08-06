
# etna-kits

The curated kit and skill repository for [Etna](https://github.com/dwhite-sys/Etna).

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

## Installing a skill

```
etna install <skill_name>
etna install <skill_name>==<version>
```

For example:
```
etna install tool-discovery
etna install visualization==1.0.0
```

## Kits

| Name | Stem | Description |
|------|------|-------------|
| Ntfy | `ntfy` | Send push notifications to a self-hosted ntfy instance |
| Web Kit | `web` | Web crawling, search, and HTTP request toolkit |
| Playwright | `playwright` | Browser automation via Chrome CDP |

## Skills

| Name | Stem | Description |
|------|------|-------------|
| Tool Discovery | `tool-discovery` | How to discover and load Etna kits, tools, and skills on demand |
| Visualization | `visualization` | Guidance for choosing and creating the right kind of visual output |

## Structure

```
etna-kits/
├── manifest.json                         ← index fetched by the Etna CLI
├── kits/
│   └── <stem>/
│       └── versions/
│           └── <version>/
│               ├── <stem>.py             ← bare kit file
│               └── <stem>.ekp            ← kit + skill bundle (preferred)
└── skills/
    └── <stem>/
        └── versions/
            └── <version>/
                └── <stem>.skill          ← skill archive
```

### .ekp packages

`.ekp` files bundle a kit and its skill documentation together. Installing an `.ekp` gives you both the tools and the model guidance for using them. Etna prefers `.ekp` over bare `.py` when both are available.

### .skill archives

`.skill` files are standalone skill documents — markdown instructions with optional reference files, scripts, and assets. They teach the model how to approach a class of tasks, independent of any specific kit.

## Contributing

This is a manually curated repository. To submit a kit or skill, open a pull request with your file(s) in the appropriate versioned directory and an updated `manifest.json` entry.
