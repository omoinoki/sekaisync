"""Independent public-entry Moly acquisition checks using source-backed finite fixtures.

The six CN/JP bodies and official talk/preAction/tweet records are exact sample
projections from the sealed first-party evidence. Transport indexes and hashes
are synthetic and bounded; these tests do not assert release or corpus coverage.
"""
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlsplit

from sekaisync import crawler, dbstore, webindex
from sekaisync.config import MoesekaiSettings


SOURCE_SAMPLES = json.loads(r'''{
  "cn": {
    "snapshot": {
      "region": "cn",
      "id": "cn-6.0.0-ad32764d95590a595c2c",
      "version": "6.0.0",
      "available": true,
      "catalog": "/moly/snapshots/cn-6.0.0-ad32764d95590a595c2c/catalog/index.json",
      "provenance": {
        "assetVersion": null,
        "catalogSha256": "7df7c95696b77297867fc09defabf8eac4f55dbed8350bea4c5aa54edc469f35"
      }
    },
    "version": {
      "systemProfile": "production",
      "appVersion": "6.4.0",
      "multiPlayVersion": "miku",
      "dataVersion": "6.4.0.10",
      "assetVersion": "6.4.0.1",
      "appHash": "d67a688b-ff78-4300-bfc5-2120c000d75d",
      "assetHash": "",
      "appVersionStatus": "available",
      "cdnVersion": 175
    },
    "samples": [
      {
        "talk": {
          "id": 3963,
          "mysekaiGameCharacterUnitGroupId": 2,
          "mysekaiCharacterTalkConditionGroupId": 70,
          "mysekaiSiteGroupId": 4,
          "mysekaiCharacterTalkTermId": 1,
          "characterArchiveMysekaiCharacterTalkGroupId": 3963,
          "assetbundleName": "mysekai/talk/scenario/talk",
          "lua": "mysekai_talk_release_002_0155",
          "isEnabledForMulti": true
        },
        "preaction": {
          "id": 3963,
          "mysekaiCharacterTalkId": 3963,
          "mysekaiCharacterTalkTweetId": 13963
        },
        "tweet": {
          "id": 13963,
          "motionName": "mov_cw_happy_nod001",
          "expressionEyeName": "smile",
          "expressionMouthName": "smile01",
          "text": "\u6211\u54e5\u54e5\u662f\u83f2\u5c3c\u4e50\u56ed\u7684\n\u821e\u53f0\u6f14\u5458\u54e6\uff01"
        },
        "detail": {
          "key": "talk:general:3963",
          "lines": [
            {
              "speaker": "\u54b2\u5e0c",
              "text": "\u6211\u54e5\u54e5\u662f\u83f2\u5c3c\u4e50\u56ed\u7684\u821e\u53f0\u6f14\u5458\u54e6\uff01"
            },
            {
              "speaker": "\u54b2\u5e0c",
              "text": "\u554a\uff0c\u4e0d\u8fc7\u4ed6\u73b0\u5728\u8fd8\u4f1a\u53bb\u522b\u7684\u5730\u65b9\u8868\u6f14\u2026\u2026\n\u4e3a\u4e86\u6210\u4e3a\u4e16\u754c\u7b2c\u4e00\u7684\u660e\u661f\uff0c\u4ed6\u771f\u7684\u975e\u5e38\u52aa\u529b\uff01"
            },
            {
              "speaker": "\u54b2\u5e0c",
              "text": "\u6240\u4ee5\u6211\u4e5f\u4f1a\u5168\u529b\u652f\u6301\u54e5\u54e5\u5b9e\u73b0\u68a6\u60f3\u7684\uff01"
            }
          ],
          "preview": {
            "available": true,
            "tweetId": 13963,
            "text": "\u6211\u54e5\u54e5\u662f\u83f2\u5c3c\u4e50\u56ed\u7684\n\u821e\u53f0\u6f14\u5458\u54e6\uff01",
            "unit": 2
          },
          "presentation": {
            "category": "conversation",
            "behavior": "authored",
            "primaryAction": "play",
            "textMode": "transcript"
          },
          "characters": [
            {
              "id": 2,
              "name": "\u5929\u9a6c\u54b2\u5e0c",
              "originalName": "\u5929\u9a6c\u54b2\u5e0c",
              "group": "Leo/need",
              "color": "#ffdd44"
            }
          ],
          "unitIds": [
            2
          ]
        }
      },
      {
        "talk": {
          "id": 1,
          "mysekaiGameCharacterUnitGroupId": 1,
          "mysekaiCharacterTalkConditionGroupId": 100005,
          "mysekaiSiteGroupId": 4,
          "mysekaiCharacterTalkTermId": 1,
          "characterArchiveMysekaiCharacterTalkGroupId": 1,
          "assetbundleName": "mysekai/talk/scenario/talk",
          "lua": "mysekai_talk_release_001_0001",
          "isEnabledForMulti": true
        },
        "preaction": {
          "id": 1,
          "mysekaiCharacterTalkId": 1,
          "mysekaiCharacterTalkTweetId": 10001
        },
        "tweet": {
          "id": 10001,
          "motionName": "mov_cw_adult_tiltheadl003",
          "expressionEyeName": "normal",
          "expressionMouthName": "smile01",
          "text": "\u8fd9\u4e2a\u6536\u7eb3\u67dc\u7684\u989c\u8272\n\u771f\u6f02\u4eae\u554a\u3002"
        },
        "detail": {
          "key": "talk:fixture:1",
          "lines": [
            {
              "speaker": "\u4e00\u6b4c",
              "text": "\u8fd9\u4e2a\u6536\u7eb3\u67dc\u7684\u989c\u8272\u771f\u6f02\u4eae\u554a\u3002\n\u62c9\u624b\u8fd8\u505a\u6210\u4e86\u661f\u661f\u7684\u5f62\u72b6\uff0c\u771f\u662f\u592a\u53ef\u7231\u4e86\u3002"
            }
          ],
          "preview": {
            "available": true,
            "tweetId": 10001,
            "text": "\u8fd9\u4e2a\u6536\u7eb3\u67dc\u7684\u989c\u8272\n\u771f\u6f02\u4eae\u554a\u3002",
            "unit": 1
          },
          "presentation": {
            "category": "fixture_story",
            "behavior": "authored",
            "primaryAction": "play",
            "textMode": "transcript"
          },
          "characters": [
            {
              "id": 1,
              "name": "\u661f\u4e43\u4e00\u6b4c",
              "originalName": "\u661f\u4e43\u4e00\u6b4c",
              "group": "Leo/need",
              "color": "#33aaee"
            }
          ],
          "unitIds": [
            1
          ]
        }
      },
      {
        "talk": {
          "id": 478,
          "mysekaiGameCharacterUnitGroupId": 4,
          "mysekaiCharacterTalkConditionGroupId": 100192,
          "mysekaiSiteGroupId": 4,
          "mysekaiCharacterTalkTermId": 1,
          "characterArchiveMysekaiCharacterTalkGroupId": 478,
          "assetbundleName": "mysekai/talk/scenario/talk",
          "lua": "mysekai_talk_release_004_0027",
          "isEnabledForMulti": true
        },
        "preaction": {
          "id": 478,
          "mysekaiCharacterTalkId": 478,
          "mysekaiCharacterTalkTweetId": 10478
        },
        "tweet": {
          "id": 10478,
          "motionName": "mov_cw_normal_nod002",
          "expressionEyeName": "normal",
          "expressionMouthName": "smile01",
          "text": "\u4ee5\u524d\u6211\u4eec\u73ed\u6709\u4e2a\u540c\u5b66\n\u7279\u522b\u4f1a\u73a9\u5355\u6760\u2026\u2026"
        },
        "detail": {
          "key": "talk:fixture:478",
          "lines": [
            {
              "speaker": "\u5fd7\u6b65",
              "text": "\u4ee5\u524d\u6211\u4eec\u73ed\u6709\u4e2a\u540c\u5b66\u7279\u522b\u4f1a\u73a9\u5355\u6760\u3002"
            },
            {
              "speaker": "\u5fd7\u6b65",
              "text": "\u6211\u5927\u6982\u5c31\u53ea\u4f1a\u811a\u8e6c\u5730\u7ffb\u8eab\u4e0a\u6760\u5427\u3002"
            }
          ],
          "preview": {
            "available": true,
            "tweetId": 10478,
            "text": "\u4ee5\u524d\u6211\u4eec\u73ed\u6709\u4e2a\u540c\u5b66\n\u7279\u522b\u4f1a\u73a9\u5355\u6760\u2026\u2026",
            "unit": 4
          },
          "presentation": {
            "category": "fixture_story",
            "behavior": "authored",
            "primaryAction": "play",
            "textMode": "transcript"
          },
          "characters": [
            {
              "id": 4,
              "name": "\u65e5\u91ce\u68ee\u5fd7\u6b65",
              "originalName": "\u65e5\u91ce\u68ee\u5fd7\u6b65",
              "group": "Leo/need",
              "color": "#bbdd22"
            }
          ],
          "unitIds": [
            4
          ]
        }
      }
    ]
  },
  "jp": {
    "snapshot": {
      "region": "jp",
      "id": "jp-6.8.1-7758ba417653d8450a78",
      "version": "6.8.1",
      "available": true,
      "catalog": "/moly/snapshots/jp-6.8.1-7758ba417653d8450a78/catalog/index.json",
      "provenance": {
        "assetVersion": "6.8.0.40",
        "catalogSha256": "5f71190edd159908c467389e9dea0c97879c169997dbfb18747e2a07bfaa8b1f"
      }
    },
    "version": {
      "appVersion": "6.8.1",
      "assetVersion": "6.8.0.60",
      "dataVersion": "6.8.0.61",
      "appHash": "20dcf972-c5be-4cb6-87af-2185db08a10a",
      "assetHash": "19989fd8-8328-4875-914b-87ec1b33c45c",
      "multiPlayVersion": "kaito",
      "systemProfile": "production",
      "appVersionStatus": "available"
    },
    "samples": [
      {
        "talk": {
          "id": 3963,
          "mysekaiGameCharacterUnitGroupId": 2,
          "mysekaiCharacterTalkConditionGroupId": 70,
          "mysekaiSiteGroupId": 4,
          "mysekaiCharacterTalkTermId": 1,
          "characterArchiveMysekaiCharacterTalkGroupId": 3963,
          "assetbundleName": "mysekai/talk/scenario/talk",
          "lua": "mysekai_talk_release_002_0155",
          "isEnabledForMulti": true
        },
        "preaction": {
          "id": 3963,
          "mysekaiCharacterTalkId": 3963,
          "mysekaiCharacterTalkTweetId": 13963
        },
        "tweet": {
          "id": 13963,
          "motionName": "mov_cw_happy_nod001",
          "expressionEyeName": "smile",
          "expressionMouthName": "smile01",
          "text": "\u30a2\u30bf\u30b7\u306e\u304a\u5144\u3061\u3083\u3093\u306d\u3001\n\u30d5\u30a7\u30cb\u30e9\u30f3\u306e\u2026\u2026"
        },
        "detail": {
          "key": "talk:general:3963",
          "lines": [
            {
              "speaker": "\u54b2\u5e0c",
              "text": "\u30a2\u30bf\u30b7\u306e\u304a\u5144\u3061\u3083\u3093\u306d\u3001\n\u30d5\u30a7\u30cb\u30e9\u30f3\u306e\u30b7\u30e7\u30fc\u30ad\u30e3\u30b9\u30c8\u306a\u3093\u3060\u3088\uff01"
            },
            {
              "speaker": "\u54b2\u5e0c",
              "text": "\u3042\u3001\u4eca\u306f\u305d\u3053\u3060\u3051\u3067\u3084\u3063\u3066\u308b\u308f\u3051\u3058\u3083\u306a\u3044\u3093\u3060\u3051\u3069\u2026\u2026\n\u4e16\u754c\u4e00\u306e\u30b9\u30bf\u30fc\u306b\u306a\u308b\u305f\u3081\u306b\u3001\u3059\u30fc\u3063\u3054\u304f\u304c\u3093\u3070\u3063\u3066\u308b\u306e\uff01"
            },
            {
              "speaker": "\u54b2\u5e0c",
              "text": "\u3060\u304b\u3089\u30a2\u30bf\u30b7\u3082\u3001\u304a\u5144\u3061\u3083\u3093\u306e\u5922\u304c\u53f6\u3044\u307e\u3059\u3088\u3046\u306b\u3063\u3066\u3001\n\u3044\u3063\u3071\u3044\u5fdc\u63f4\u3057\u3066\u308b\u3093\u3060\uff01"
            }
          ],
          "preview": {
            "available": true,
            "tweetId": 13963,
            "text": "\u30a2\u30bf\u30b7\u306e\u304a\u5144\u3061\u3083\u3093\u306d\u3001\n\u30d5\u30a7\u30cb\u30e9\u30f3\u306e\u2026\u2026",
            "unit": 2
          },
          "presentation": {
            "category": "conversation",
            "behavior": "authored",
            "primaryAction": "play",
            "textMode": "transcript"
          },
          "characters": [
            {
              "id": 2,
              "name": "\u5929\u99ac\u54b2\u5e0c",
              "originalName": "\u5929\u99ac\u54b2\u5e0c",
              "group": "Leo/need",
              "color": "#ffdd44"
            }
          ],
          "unitIds": [
            2
          ]
        }
      },
      {
        "talk": {
          "id": 1,
          "mysekaiGameCharacterUnitGroupId": 1,
          "mysekaiCharacterTalkConditionGroupId": 100005,
          "mysekaiSiteGroupId": 4,
          "mysekaiCharacterTalkTermId": 1,
          "characterArchiveMysekaiCharacterTalkGroupId": 1,
          "assetbundleName": "mysekai/talk/scenario/talk",
          "lua": "mysekai_talk_release_001_0001",
          "isEnabledForMulti": true
        },
        "preaction": {
          "id": 1,
          "mysekaiCharacterTalkId": 1,
          "mysekaiCharacterTalkTweetId": 10001
        },
        "tweet": {
          "id": 10001,
          "motionName": "mov_cw_adult_tiltheadl003",
          "expressionEyeName": "normal",
          "expressionMouthName": "smile01",
          "text": "\u7dba\u9e97\u306a\u8272\u306e\n\u30c1\u30a7\u30b9\u30c8\u3060\u306d"
        },
        "detail": {
          "key": "talk:fixture:1",
          "lines": [
            {
              "speaker": "\u4e00\u6b4c",
              "text": "\u7dba\u9e97\u306a\u8272\u306e\u30c1\u30a7\u30b9\u30c8\u3060\u306d\u3002\n\u53d6\u3063\u624b\u306e\u3068\u3053\u308d\u304c\u661f\u306b\u306a\u3063\u3066\u3066\u3001\u53ef\u611b\u3044\u306a"
            }
          ],
          "preview": {
            "available": true,
            "tweetId": 10001,
            "text": "\u7dba\u9e97\u306a\u8272\u306e\n\u30c1\u30a7\u30b9\u30c8\u3060\u306d",
            "unit": 1
          },
          "presentation": {
            "category": "fixture_story",
            "behavior": "authored",
            "primaryAction": "play",
            "textMode": "transcript"
          },
          "characters": [
            {
              "id": 1,
              "name": "\u661f\u4e43\u4e00\u6b4c",
              "originalName": "\u661f\u4e43\u4e00\u6b4c",
              "group": "Leo/need",
              "color": "#33aaee"
            }
          ],
          "unitIds": [
            1
          ]
        }
      },
      {
        "talk": {
          "id": 478,
          "mysekaiGameCharacterUnitGroupId": 4,
          "mysekaiCharacterTalkConditionGroupId": 100192,
          "mysekaiSiteGroupId": 4,
          "mysekaiCharacterTalkTermId": 1,
          "characterArchiveMysekaiCharacterTalkGroupId": 478,
          "assetbundleName": "mysekai/talk/scenario/talk",
          "lua": "mysekai_talk_release_004_0027",
          "isEnabledForMulti": true
        },
        "preaction": {
          "id": 478,
          "mysekaiCharacterTalkId": 478,
          "mysekaiCharacterTalkTweetId": 10478
        },
        "tweet": {
          "id": 10478,
          "motionName": "mov_cw_normal_nod002",
          "expressionEyeName": "normal",
          "expressionMouthName": "smile01",
          "text": "\u30af\u30e9\u30b9\u3067\u3072\u3068\u308a\u306f\u3001\n\u9244\u68d2\u304c\u7570\u69d8\u306b\u4e0a\u624b\u306a\u2026\u2026"
        },
        "detail": {
          "key": "talk:fixture:478",
          "lines": [
            {
              "speaker": "\u5fd7\u6b69",
              "text": "\u30af\u30e9\u30b9\u3067\u3072\u3068\u308a\u306f\u3001\n\u9244\u68d2\u304c\u7570\u69d8\u306b\u4e0a\u624b\u306a\u5b50\u3063\u3066\u3044\u305f\u3088\u306d"
            },
            {
              "speaker": "\u5fd7\u6b69",
              "text": "\u79c1\u306b\u3067\u304d\u308b\u306e\u306f\u3001\u8db3\u639b\u3051\u524d\u8ee2\u304f\u3089\u3044\u304b\u306a"
            }
          ],
          "preview": {
            "available": true,
            "tweetId": 10478,
            "text": "\u30af\u30e9\u30b9\u3067\u3072\u3068\u308a\u306f\u3001\n\u9244\u68d2\u304c\u7570\u69d8\u306b\u4e0a\u624b\u306a\u2026\u2026",
            "unit": 4
          },
          "presentation": {
            "category": "fixture_story",
            "behavior": "authored",
            "primaryAction": "play",
            "textMode": "transcript"
          },
          "characters": [
            {
              "id": 4,
              "name": "\u65e5\u91ce\u68ee\u5fd7\u6b69",
              "originalName": "\u65e5\u91ce\u68ee\u5fd7\u6b69",
              "group": "Leo/need",
              "color": "#bbdd22"
            }
          ],
          "unitIds": [
            4
          ]
        }
      }
    ]
  }
}''')


class _MolyPublicFixture:
    def __init__(self, owner):
        self.owner = owner
        temporary = tempfile.TemporaryDirectory()
        owner.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name) / "store"
        dbstore.initialize(self.store)
        self.data = deepcopy(SOURCE_SAMPLES)
        self.calls = []
        self.lock = threading.Lock()
        self.metadata_suffixes = ("", ".json")
        self.metadata_table_suffixes = {}
        self.raw_overrides = {}
        self.settings = MoesekaiSettings(
            site_base="https://moly-review.invalid",
            metadata_bases=("https://moly-review.invalid/metadata",),
            asset_bases=("https://moly-review.invalid/legacy-assets",),
            fallback_to_viewer_cdn=False,
        )
        self.catalogs = {}
        self.bundles = {}
        self.payloads = {}
        self.rebuild()

    def rebuild(self):
        self.payloads = {}
        snapshots = []
        for region, source in self.data.items():
            snapshot = source["snapshot"]
            snapshots.append(snapshot)
            details = {sample["detail"]["key"]: deepcopy(sample["detail"]) for sample in source["samples"]}
            blob = json.dumps(dict(schemaVersion=2, entries=details), ensure_ascii=False).encode("utf-8")
            logical = "/moly/catalog-store/" + hashlib.sha256(blob).hexdigest() + ".json"
            catalog = dict(schemaVersion=2, region=region, version=snapshot["version"],
                           snapshotId=snapshot["id"], details=[logical], entries=[])
            for sample in source["samples"]:
                entry = deepcopy(sample["detail"])
                entry.pop("lines")
                entry.update(detail=0, available=True, title="Not a dialogue body", subtitle="Not a dialogue body")
                catalog["entries"].append(entry)
            self.catalogs[region] = catalog
            self.bundles[region] = logical
            self.payloads[logical] = blob.decode("utf-8")
            self.payloads[snapshot["catalog"]] = json.dumps(catalog, ensure_ascii=False)
        self.manifest = dict(schemaVersion=2, snapshots=snapshots)
        self.publish_manifest()

    def publish_manifest(self):
        self.payloads["/moly/manifest.json"] = json.dumps(self.manifest, ensure_ascii=False)

    def publish_catalog(self, region):
        self.payloads[self.data[region]["snapshot"]["catalog"]] = json.dumps(self.catalogs[region], ensure_ascii=False)

    def fetch(self, url):
        parsed = urlsplit(url)
        self.owner.assertEqual(parsed.hostname, "moly-review.invalid", "Configured source binding escaped the finite host")
        with self.lock:
            self.calls.append(parsed.path)
        if parsed.path.startswith("/metadata/"):
            parts = parsed.path.strip("/").split("/")
            self.owner.assertEqual(parts[2], "master")
            region, table = parts[1], parts[3].removesuffix(".json")
            suffix = ".json" if parts[3].endswith(".json") else ""
            if suffix not in self.metadata_table_suffixes.get(table, self.metadata_suffixes):
                raise HTTPError(url, 404, "This metadata route is not exposed by the finite provider", {}, None)
            source = self.data.get(region)
            if not source:
                return "[]"
            if table == "versions":
                return json.dumps(source["version"], ensure_ascii=False)
            if (region, table) in self.raw_overrides:
                return json.dumps(self.raw_overrides[region, table], ensure_ascii=False)
            fields = {
                "mysekaiCharacterTalks": "talk",
                "mysekaiCharacterTalkPreActions": "preaction",
                "mysekaiCharacterTalkTweets": "tweet",
            }
            if table in fields:
                return json.dumps([sample[fields[table]] for sample in source["samples"]], ensure_ascii=False)
            return "[]"
        if parsed.path in self.payloads:
            return self.payloads[parsed.path]
        raise HTTPError(url, 404, "Not in the finite public-entry transport", {}, None)

    def crawl(self, *, locale="zh-cn", locales=None, limit=0, resume=False, workers=1):
        def deny_network(*args, **kwargs):
            self.owner.fail("The injected public fixture attempted actual networking")
        with mock.patch.object(crawler, "fetch_http_text", side_effect=deny_network), \
                mock.patch("urllib.request.urlopen", side_effect=deny_network), \
                mock.patch("socket.create_connection", side_effect=deny_network), redirect_stdout(io.StringIO()):
            result = crawler.crawl_altsource_ms(self.store, depth=4, locales=locales or (locale,),
                limit=limit, accept_tos=True, delay=0, workers=workers, resume=resume,
                include_overlay=False, settings=self.settings, fetcher=self.fetch)
        return result, self.talks()

    def talks(self):
        return [page for page in webindex.load_web_pages(self.store).get("altsource_ms", [])
                if page.get("kind") == "mysekai_talk"]

    def body_counts(self):
        return Counter(path for path in self.calls if path.startswith("/moly/catalog-store/"))

    def moly_metadata_calls(self):
        start = self.calls.index("/moly/manifest.json") + 1
        stop = next((index for index in range(start, len(self.calls))
                     if self.calls[index].startswith("/moly/catalog-store/")
                     or self.calls[index] == "/metadata/cn/master/eventStories.json"), len(self.calls))
        return [path for path in self.calls[start:stop] if path.startswith("/metadata/")]

    def expected(self, region="cn"):
        return {str(sample["talk"]["id"]): "\n".join(
                    line["speaker"] + "\uff1a" + line["text"] for line in sample["detail"]["lines"])
                for sample in self.data[region]["samples"]}

    def assert_complete(self, pages, *, region="cn", identities=None):
        expected = self.expected(region)
        identities = set(expected) if identities is None else set(identities)
        self.owner.assertEqual({page["id"].rsplit(":", 1)[-1] for page in pages}, identities)
        locale, language = ("zh-cn", "zh_hans") if region == "cn" else ("ja-jp", "ja")
        for page in pages:
            identity = page["id"].rsplit(":", 1)[-1]
            self.owner.assertEqual(page["id"], f"web:altsource_ms:{locale}:mysekai_talk:{identity}")
            self.owner.assertEqual(page["text"], expected[identity])
            self.owner.assertEqual(page["source"], "altsource_ms")
            self.owner.assertEqual(page["language"], language)
            self.owner.assertEqual(page["hash"], hashlib.sha1(page["text"].encode("utf-8")).hexdigest()[:16])


class MysekaiMolyCompletenessReviewTests(unittest.TestCase):
    def test_real_cn_and_jp_full_bodies_keep_all_speakers_lines_and_existing_locales(self):
        for region, locale in (("cn", "zh-cn"), ("jp", "ja-jp")):
            for workers in (1, 4):
                with self.subTest(region=region, workers=workers):
                    fixture = _MolyPublicFixture(self)
                    _, pages = fixture.crawl(locale=locale, workers=workers)
                    fixture.assert_complete(pages, region=region)
                    self.assertEqual(sum(fixture.body_counts().values()), 1, "Shared detail bundles must not be fetched per official ID")

    def test_limit_one_is_a_full_body_not_title_tweet_or_raw_definition(self):
        for workers in (1, 4):
            fixture = _MolyPublicFixture(self)
            result, pages = fixture.crawl(limit=1, workers=workers)
            self.assertEqual(result["crawled_pages"], 1)
            self.assertEqual(len(pages), 1)
            fixture.assert_complete(pages, identities={pages[0]["id"].rsplit(":", 1)[-1]})

    def test_resume_known_subset_acquires_later_ids_without_spending_budget_on_known_body(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl(limit=1)
        before = {page["id"]: deepcopy(page) for page in fixture.talks()}
        result, pages = fixture.crawl(limit=1, resume=True)
        self.assertEqual(result["crawled_pages"], 1)
        self.assertEqual(len(pages), 2)
        fixture.assert_complete(pages, identities={page["id"].rsplit(":", 1)[-1] for page in pages})
        after = {page["id"]: page for page in pages}
        self.assertEqual({identity: after[identity] for identity in before}, before)

    def test_all_known_valid_bodies_resume_without_detail_fetch_or_page_metadata_changes(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl()
        before = {page["id"]: deepcopy(page) for page in fixture.talks()}
        fixture.calls.clear()
        result, pages = fixture.crawl(resume=True)
        self.assertEqual(result["crawled_pages"], 0)
        fixture.assert_complete(pages)
        self.assertEqual(fixture.body_counts(), {})
        self.assertEqual({page["id"]: page for page in pages}, before)

    def test_existing_raw_definition_ids_do_not_discharge_missing_body_ids(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl()
        with dbstore.connect(fixture.store) as connection:
            connection.execute("DELETE FROM web_pages WHERE source = ? AND kind = ?", ("altsource_ms", "mysekai_talk"))
            connection.commit()
        _, pages = fixture.crawl(resume=True)
        fixture.assert_complete(pages)

    def test_flagged_body_is_retried_on_resume(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl()
        with dbstore.connect(fixture.store) as connection:
            connection.execute("UPDATE web_pages SET content_language_mismatch = 1 WHERE source = ? AND kind = ?", ("altsource_ms", "mysekai_talk"))
            connection.commit()
        fixture.calls.clear()
        _, pages = fixture.crawl(resume=True)
        fixture.assert_complete(pages)
        self.assertTrue(fixture.body_counts())
        self.assertTrue(all(not page["content_language_mismatch"] for page in pages))

    def test_missing_overseas_snapshot_never_uses_cn_or_jp_body(self):
        for locale in ("en-us", "zh-tw", "ko-kr"):
            fixture = _MolyPublicFixture(self)
            _, pages = fixture.crawl(locale=locale)
            self.assertEqual(pages, [])
            self.assertEqual(fixture.body_counts(), {})

    def test_wrong_catalog_region_snapshot_or_version_cannot_supply_cn_body(self):
        for field, value in (("region", "jp"), ("snapshotId", "unrelated"), ("version", "0.0.0")):
            fixture = _MolyPublicFixture(self)
            fixture.catalogs["cn"][field] = value
            fixture.publish_catalog("cn")
            _, pages = fixture.crawl()
            self.assertEqual(pages, [])
            self.assertEqual(fixture.body_counts(), {})

    def test_bad_detail_sha_cannot_supply_body_even_if_lines_look_correct(self):
        fixture = _MolyPublicFixture(self)
        fixture.payloads[fixture.bundles["cn"]] += "\n"
        _, pages = fixture.crawl()
        self.assertEqual(pages, [])

    def test_tweet_only_early_ids_do_not_hide_later_full_body_with_budget_one(self):
        fixture = _MolyPublicFixture(self)
        for sample in fixture.data["cn"]["samples"]:
            if sample["talk"]["id"] in (1, 478):
                sample["detail"]["lines"] = [dict(speaker=sample["detail"]["lines"][0]["speaker"], text=sample["tweet"]["text"])]
        fixture.rebuild()
        result, pages = fixture.crawl(limit=1)
        self.assertEqual(result["crawled_pages"], 1)
        fixture.assert_complete(pages, identities={"3963"})

    def test_same_body_distinct_official_ids_remain_distinct_pages(self):
        fixture = _MolyPublicFixture(self)
        first = next(sample for sample in fixture.data["cn"]["samples"] if sample["talk"]["id"] == 1)
        second = next(sample for sample in fixture.data["cn"]["samples"] if sample["talk"]["id"] == 478)
        second["detail"]["lines"] = deepcopy(first["detail"]["lines"])
        fixture.rebuild()
        _, pages = fixture.crawl()
        fixture.assert_complete(pages)
        by_id = {page["id"].rsplit(":", 1)[-1]: page for page in pages}
        self.assertEqual(by_id["1"]["text"], by_id["478"]["text"])
        self.assertNotEqual(by_id["1"]["id"], by_id["478"]["id"])

    def test_changed_bundle_on_resume_does_not_keep_old_body_due_to_numeric_known_id(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl()
        second = next(sample for sample in fixture.data["cn"]["samples"] if sample["talk"]["id"] == 478)
        second["detail"]["lines"][1]["text"] += "\n\u8fd9\u662f\u65b0\u7684\u6b63\u6587\u884c\u3002"
        fixture.rebuild()
        _, pages = fixture.crawl(resume=True)
        fixture.assert_complete(pages)

    def test_changed_snapshot_identity_is_not_the_same_known_body_receipt(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl()
        before = {page["id"]: page["source_hash"] for page in fixture.talks()}
        fixture.data["cn"]["snapshot"]["id"] += "-next"
        fixture.data["cn"]["snapshot"]["catalog"] = "/moly/snapshots/" + fixture.data["cn"]["snapshot"]["id"] + "/catalog/index.json"
        fixture.rebuild()
        _, pages = fixture.crawl(resume=True)
        fixture.assert_complete(pages)
        self.assertTrue(all(page["source_hash"] != before[page["id"]] for page in pages))

    def test_duplicate_or_ambiguous_catalog_keys_never_merge_numeric_identities(self):
        for key in ("talk:fixture:1", "talk:general:1"):
            fixture = _MolyPublicFixture(self)
            entry = deepcopy(next(row for row in fixture.catalogs["cn"]["entries"] if row["key"] == "talk:fixture:1"))
            entry["key"] = key
            fixture.catalogs["cn"]["entries"].append(entry)
            fixture.publish_catalog("cn")
            _, pages = fixture.crawl()
            self.assertNotIn("1", {page["id"].rsplit(":", 1)[-1] for page in pages})

    def test_external_or_traversal_detail_paths_are_not_requested(self):
        for path in ("https://other.invalid/x.json", "/moly/catalog-store/../x.json"):
            fixture = _MolyPublicFixture(self)
            fixture.catalogs["cn"]["details"][0] = path
            fixture.publish_catalog("cn")
            _, pages = fixture.crawl()
            self.assertEqual(pages, [])
            self.assertEqual(fixture.body_counts(), {})

    def test_limit_one_workers_four_sidecar_only_records_persisted_body_ids(self):
        fixture = _MolyPublicFixture(self)
        _, pages = fixture.crawl(limit=1, workers=4)
        self.assertEqual(len(pages), 1)
        sidecar = fixture.store / "cache" / "altsource_ms" / "mysekai_moly_provenance.json"
        receipts = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(set(receipts), {page["id"] for page in pages},
                         "Fetched speculative workers must not mint persisted-body receipts")

    def test_resume_repairs_local_text_damage_even_when_source_binding_hash_is_unchanged(self):
        fixture = _MolyPublicFixture(self)
        fixture.crawl()
        with dbstore.connect(fixture.store) as connection:
            connection.execute("UPDATE web_pages SET text = ? WHERE source = ? AND kind = ? AND id LIKE ?",
                               ("damaged local body", "altsource_ms", "mysekai_talk", "%:478"))
            connection.commit()
        fixture.calls.clear()
        _, pages = fixture.crawl(resume=True)
        fixture.assert_complete(pages)
        self.assertTrue(fixture.body_counts(), "Local damage requires verified source-body recovery")

    def test_malformed_snapshot_provenance_never_crashes_or_hides_other_valid_region_bodies(self):
        for provenance in (None, "not an object", []):
            with self.subTest(provenance=provenance):
                fixture = _MolyPublicFixture(self)
                fixture.data["cn"]["snapshot"]["provenance"] = provenance
                fixture.publish_manifest()
                _, pages = fixture.crawl(locales=("zh-cn", "ja-jp"))
                self.assertEqual([page for page in pages if page["language"] == "zh_hans"], [])
                fixture.assert_complete([page for page in pages if page["language"] == "ja"], region="jp")
                self.assertNotIn(fixture.bundles["cn"], fixture.body_counts(),
                                 "Malformed-source candidates must not mint body pages or spend body acquisition budget")

    def test_oversized_numeric_catalog_key_does_not_hide_existing_valid_bodies(self):
        fixture = _MolyPublicFixture(self)
        invalid = deepcopy(fixture.catalogs["cn"]["entries"][0])
        invalid["key"] = "talk:general:" + "9" * 5000
        fixture.catalogs["cn"]["entries"].insert(0, invalid)
        fixture.publish_catalog("cn")
        _, pages = fixture.crawl()
        fixture.assert_complete(pages)

    def test_json_only_metadata_acquires_existing_complete_bodies(self):
        fixture = _MolyPublicFixture(self)
        fixture.metadata_suffixes = (".json",)
        _, pages = fixture.crawl()
        fixture.assert_complete(pages)
        expected = ["/metadata/cn/master/" + table + suffix for table in (
            "mysekaiCharacterTalks", "mysekaiCharacterTalkTweets", "mysekaiCharacterTalkPreActions", "versions")
            for suffix in ("", ".json")]
        self.assertEqual(fixture.moly_metadata_calls(), expected)

    def test_extensionless_metadata_does_not_request_successful_table_aliases(self):
        fixture = _MolyPublicFixture(self)
        fixture.metadata_suffixes = ("",)
        _, pages = fixture.crawl()
        fixture.assert_complete(pages)
        self.assertEqual(len(fixture.moly_metadata_calls()), 4)
        self.assertTrue(all(not path.endswith(".json") for path in fixture.moly_metadata_calls()))

    def test_both_metadata_routes_preserve_first_route_and_unique_bundle_work(self):
        fixture = _MolyPublicFixture(self)
        _, pages = fixture.crawl(workers=4)
        fixture.assert_complete(pages)
        self.assertEqual(len(fixture.moly_metadata_calls()), 4)
        self.assertEqual(fixture.body_counts(), {fixture.bundles["cn"]: 1})

    def test_json_only_metadata_aliases_do_not_spend_saved_body_budget(self):
        fixture = _MolyPublicFixture(self)
        fixture.metadata_suffixes = (".json",)
        _, pages = fixture.crawl(limit=1, workers=4)
        self.assertEqual(len(pages), 1)
        sidecar = fixture.store / "cache/altsource_ms/mysekai_moly_provenance.json"
        self.assertEqual(set(json.loads(sidecar.read_text(encoding="utf-8"))), {pages[0]["id"]})
        self.assertEqual(len(fixture.moly_metadata_calls()), 8)

    def test_json_only_versions_are_preserved_in_actual_body_proofs(self):
        fixture = _MolyPublicFixture(self)
        fixture.metadata_table_suffixes["versions"] = (".json",)
        _, pages = fixture.crawl()
        fixture.assert_complete(pages)
        sidecar = fixture.store / "cache/altsource_ms/mysekai_moly_provenance.json"
        proofs = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertTrue(all(proof["raw_version"] == fixture.data["cn"]["version"] for proof in proofs.values()))
        self.assertEqual(fixture.moly_metadata_calls()[-2:],
                         ["/metadata/cn/master/versions", "/metadata/cn/master/versions.json"])

    def test_valid_empty_primary_table_is_not_replaced_by_a_nonempty_alias(self):
        fixture = _MolyPublicFixture(self)
        original = fixture.fetch
        def fetch(url):
            if urlsplit(url).path == "/metadata/cn/master/mysekaiCharacterTalks":
                with fixture.lock:
                    fixture.calls.append(urlsplit(url).path)
                return "[]"
            return original(url)
        fixture.fetch = fetch
        _, pages = fixture.crawl()
        self.assertEqual(pages, [])
        self.assertNotIn("/metadata/cn/master/mysekaiCharacterTalks.json", fixture.moly_metadata_calls())
        self.assertEqual(fixture.body_counts(), {})

    def test_injected_fatal_stop_does_not_turn_into_more_metadata_alias_requests(self):
        fixture = _MolyPublicFixture(self)
        original = fixture.fetch
        def fetch(url):
            if urlsplit(url).path == "/metadata/cn/master/mysekaiCharacterTalks":
                with fixture.lock:
                    fixture.calls.append(urlsplit(url).path)
                raise RuntimeError("Injected request budget exhausted")
            return original(url)
        fixture.fetch = fetch
        with self.assertRaisesRegex(RuntimeError, "Injected request budget exhausted"):
            fixture.crawl()
        self.assertEqual(fixture.moly_metadata_calls(), ["/metadata/cn/master/mysekaiCharacterTalks"])

    def test_shared_preview_condition_alias_ids_keep_distinct_actual_body_proofs(self):
        fixture = _MolyPublicFixture(self)
        samples = fixture.data["cn"]["samples"]
        original = next(sample for sample in samples if sample["talk"]["id"] == 1)
        alias = deepcopy(original)
        alias["talk"]["id"] = 100001
        alias["preaction"].update(id=100001, mysekaiCharacterTalkId=100001)
        alias["detail"]["key"] = "talk:fixture:100001"
        samples.append(alias)
        fixture.raw_overrides["cn", "mysekaiCharacterTalkTweets"] = [
            sample["tweet"] for sample in samples if sample is not alias]
        fixture.rebuild()
        _, pages = fixture.crawl(workers=4)
        fixture.assert_complete(pages)
        by_id = {page["id"].rsplit(":", 1)[-1]: page for page in pages}
        self.assertEqual(by_id["1"]["text"], by_id["100001"]["text"])
        self.assertNotEqual(by_id["1"]["source_hash"], by_id["100001"]["source_hash"])
        sidecar = fixture.store / "cache/altsource_ms/mysekai_moly_provenance.json"
        proofs = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(len(proofs), 4)
        self.assertEqual(proofs[by_id["100001"]["id"]]["raw_preaction"]["mysekaiCharacterTalkId"], 100001)
        self.assertEqual(proofs[by_id["1"]["id"]]["raw_preview_tweet"],
                         proofs[by_id["100001"]["id"]]["raw_preview_tweet"])


if __name__ == "__main__":
    unittest.main()

