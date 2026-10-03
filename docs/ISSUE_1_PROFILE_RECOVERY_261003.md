# Recover Missing Character-Profile Descriptions

SekaiSync 0.4.2-alpha restores character-profile descriptions and character-name lookup. If an existing store was synchronized with older extraction code, sync it again with the updated backend to recover those fields.

## Update And Sync

Update your SekaiSync installation using the [installation instructions](../README.md#readme-section-02), then run:

```sh
python -m sekaisync --no-event-check sync --regions jp,en,cn,tc,kr
```

If your store is outside the current directory, add `--store PATH` before `sync`. Restart running MCP and HTTP services after the update so they load the new code.

## Offline Recovery

Use `sync --local REGION=PATH` with local raw master-data directories. Include every region you want to retain. For example, with five regional directories under `./master/`:

```sh
python -m sekaisync --no-event-check sync --regions jp,en,cn,tc,kr --local jp=./master/jp --local en=./master/en --local cn=./master/cn --local tc=./master/tc --local kr=./master/kr
```

Point these paths to the raw master JSON tables, rather than an old generated `registry.json`.

## Check The Result

```sh
python -m sekaisync --no-event-check lookup --query "character_profile:18" --type character_profile
python -m sekaisync --no-event-check factpack --id character_profile:18 --language ja --region jp
```

You can also search by character name or a phrase from the profile. `--region` selects the server, while `--language` requests the output language. The FactPack reports the selected region and actual body language; use `zh_tw` or `zh_hant` for Traditional Chinese, and `zh_cn` or `zh_hans` for Simplified Chinese.
