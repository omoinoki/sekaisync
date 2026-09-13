# 用语权重总表

> 生成自 `store/kb/terms.json`（`--all --local --source-language ja` 全量抽取）。
> `weight = log1p(occurrences) * trust_factor * tag_prior`
> `trust: A 1.3 / B 1.1 / C 1.0 / D 0.9`，`tag_prior: person 1.2 / event 1.15 / product 1.05 / location 1.0 / organization 1.0 / other 0.7`（重叠取 max）。

## 概览

- 全量用语：381 条（官方 105，带证据 329）
- 仅日语（无他服译名，含 JP 领先期新增名词）：0 条（0.0%）
- 多语对照：381 条
- 各语种覆盖：ja 381, zh_hans 182, en 331, zh_tw 59, ko 138
- 各 tag 计数：event 22, location 85, organization 11, other 240, person 26, product 4
- 权重范围：0.00 – 1.08，中位 0.49

**注意**：`only-ja` 不等于错误——日服显著领先，许多新活动/新卡面名词确实只有日语。需人工判断是否待他服更新后补译。

## 各 Tag 权重前 5

| tag | canonical | weight | occ | langs | 对照（zh_hans / en） |
|-----|-----------|--------|-----|-------|----------------------|
| event | ライブハウス | 0.80 | 1 | en,ja,ko,zh_hans,zh_tw | Live House / Live House |
| event | ソロライブ | 0.80 | 1 | en,ja,ko,zh_hans,zh_tw | 个人演唱会 / Solo Live |
| event | ファンフェスタ | 0.80 | 1 | en,ja,zh_hans | 粉丝节 / SINGER music |
| event | ワンマンライブ | 0.80 | 1 | en,ja,ko,zh_hans,zh_tw | 专场演唱会 / Solo Live |
| event | コンテスト | 0.80 | 1 | en,ja,ko,zh_hans,zh_tw | 大赛 / Contest |
| location | スクランブル交差点 | 0.90 | 1 | en,ja,ko,name,zh_hans,zh_hant | 全向十字路口 / Scramble Crossing |
| location | 誰もいないセカイ | 0.90 | 1 | en,ja,ko,name,zh_hans,zh_hant | 无人「世界」 / Empty SEKAI |
| location | テーマパーク | 0.90 | 1 | en,ja,ko,name,zh_hans,zh_hant | 主题乐园 / Theme Park |
| location | 宮益坂女子学園 | 0.90 | 1 | en,ja,ko,name,zh_hans,zh_hant | 宫益坂女子学园 / Miyamasuzaka Girls Academy |
| location | ステージのセカイ | 0.90 | 1 | en,ja,ko,name,zh_hans,zh_hant | 舞台「世界」 / Stage SEKAI |
| organization | バーチャル・シンガー | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | 虚拟歌手 / VIRTUAL SINGER |
| organization | Leo/need | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Leo/need / Leo/need |
| organization | MORE MORE JUMP！ | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | MORE MORE JUMP！ / MORE MORE JUMP! |
| organization | Vivid BAD SQUAD | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Vivid BAD SQUAD / Vivid BAD SQUAD |
| organization | ワンダーランズ×ショウタイム | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Wonderlands×Showtime / Wonderlands×Showtime |
| other | MEIKO | 1.08 | 1 | en,givenName,givenNameEnglish,ja |  / MEIKO |
| other | KAITO | 1.08 | 1 | en,givenName,givenNameEnglish,ja |  / KAITO |
| other | Leo/need | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Leo/need / Leo/need |
| other | MORE MORE JUMP！ | 0.90 | 1 | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | MORE MORE JUMP！ / MORE MORE JUMP! |
| other | デッサン | 0.49 | 1 | ja,zh_hans | 完成才行 /  |
| person | 星乃一歌 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 星乃一歌 / HOSHINO ICHIKA |
| person | 白石杏 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 白石杏 / SHIRAISHI AN |
| person | 東雲彰人 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 东云彰人 / SHINONOME AKITO |
| person | 青柳冬弥 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 青柳冬弥 / AOYAGI TOYA |
| person | 天馬司 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 天马司 / TENMA TSUKASA |
| product | フェニックスワンダーランド | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 菲尼克斯奇幻乐园 / Phoenix Wonderland |
| product | ポップアップストア | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 快闪店 / Pop-up Store |
| product | ライリードリームパーク | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 莱利梦幻乐园 / Riley Dream Park |
| product | ネットパラダイス | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 网络天堂 / NetParadise |

## 仅日语（only-ja）权重前 20

| # | canonical | weight | occ | tags | evidence |
|---|-----------|--------|-----|------|----------|

## 全量权重表 Top 300

| # | canonical | weight | occ | tags | trust | langs | zh_hans | en | evidence |
|---|-----------|--------|-----|------|-------|-------|---------|----|----|
| 1 | 星乃一歌 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 星乃一歌 | HOSHINO ICHIKA | 一歌：えっと……咲希とは幼馴染みで、宮女１年の星乃一歌です。 |
| 2 | 白石杏 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 白石杏 | SHIRAISHI AN | 杏：同じく、期間限定のお試しメンバーの白石杏です！ |
| 3 | 東雲彰人 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 东云彰人 | SHINONOME AKITO | 司：というわけで——オレは東雲彰人！！ |
| 4 | 青柳冬弥 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 青柳冬弥 | AOYAGI TOYA | 類：青柳冬弥くんだよ♪ |
| 5 | 天馬司 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 天马司 | TENMA TSUKASA | 世界を司ると書き、司！　その名も――天馬司！ |
| 6 | 鳳えむ | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 凤笑梦 | OTORI EMU | えむ：い、１年の鳳えむです！ |
| 7 | 草薙寧々 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 草薙宁宁 | KUSANAGI NENE | 寧々：えっと……神高１年、草薙寧々。 |
| 8 | 神代類 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 神代类 | KAMISHIRO RUI | 類：はい、もしもし神代類だよ。 |
| 9 | 宵崎奏 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 宵崎奏 | YOISAKI KANADE | 作曲担当……のはずだった宵崎奏です |
| 10 | 朝比奈まふゆ | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 朝比奈真冬 | ASAHINA MAFUYU | まふゆ：はじめまして、２年の朝比奈まふゆです |
| 11 | 東雲絵名 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 东云绘名 | SHINONOME ENA | 私は東雲絵名。神高の２年で、夜間クラスに通ってるよ |
| 12 | 天馬咲希 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 天马咲希 | TENMA SAKI | 咲希：はい！　宮女１年、天馬咲希です！ |
| 13 | 暁山瑞希 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 晓山瑞希 | AKIYAMA MIZUKI | 瑞希：ボクがサークル主の、神高１年、暁山瑞希。 |
| 14 | 初音ミク | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 初音未来 | HATSUNE MIKU | 彰人：いや、やっぱオレが知ってる初音ミクと姿も |
| 15 | 鏡音リン | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 镜音铃 | KAGAMINE RIN | 勇気も凛々♪　鏡音リンです！ |
| 16 | 鏡音レン | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 镜音连 | KAGAMINE LEN | オレは鏡音レン、よろしく！ |
| 17 | 巡音ルカ | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 巡音流歌 | MEGURINE LUKA | バーチャル・シンガーの巡音ルカさんです！ |
| 18 | 望月穂波 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 望月穗波 | MOCHIZUKI HONAMI | 宮女１年、望月穂波です |
| 19 | 日野森志歩 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 日野森志步 | HINOMORI SHIHO | 志歩：YUME YUME JUMP！の日野森志歩です。 |
| 20 | 花里みのり | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 花里实乃理 | HANASATO MINORI | みのり：続きまして、宮女１年花里みのりです！ |
| 21 | 桐谷遥 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 桐谷遥 | KIRITANI HARUKA | すぐ桐谷遥だってバレて、慌てて逃げたりしてたよねー。 |
| 22 | 桃井愛莉 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 桃井爱莉 | MOMOI AIRI | 愛莉：ベース担当の、宮女２年の桃井愛莉よ。 |
| 23 | 日野森雫 | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 日野森雫 | HINOMORI SHIZUKU | YUME YUME JUMP！のメンバー、日野森雫です |
| 24 | 小豆沢こはね | 1.08 | 1 | person | A | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 小豆泽心羽 | AZUSAWA KOHANE | こはね：あ、えっと……１年の小豆沢こはねです。 |
| 25 | MEIKO | 1.08 | 1 | other,person | A | en,givenName,givenNameEnglish,ja |  | MEIKO | MEIKO：コーヒーって、ちょっと手間をかけるだけで、 |
| 26 | KAITO | 1.08 | 1 | other,person | A | en,givenName,givenNameEnglish,ja |  | KAITO | KAITO：それではみなさん！ |
| 27 | スクランブル交差点 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 全向十字路口 | Scramble Crossing | 類：さあ、スクランブル交差点でのショーだ！ |
| 28 | 誰もいないセカイ | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 无人「世界」 | Empty SEKAI | だからここは、もう誰もいないセカイじゃない |
| 29 | テーマパーク | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 主题乐园 | Theme Park | 司：前から思っていたが、このテーマパークのマスコットは |
| 30 | 宮益坂女子学園 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 宫益坂女子学园 | Miyamasuzaka Girls A | みのり：宮益坂女子学園の飼育委員の名に恥じぬよう、花里みのり—— |
| 31 | ステージのセカイ | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 舞台「世界」 | Stage SEKAI | ステージのセカイのミク：『——ねえ、ルカちゃん。どんな魔法を使ったの？』 |
| 32 | センター街 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 中央街 | Main Street | センター街？　ショッピングモール？ |
| 33 | ショッピングモール | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 购物中心 | Shopping Mall | 愛莉：えっ、遥、ショッピングモールで買い物したことないの！？ |
| 34 | ミクのCD | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 初音未来的CD | Miku CDs | ミクのCDコーナーに、いっぱい人がいるなって思って |
| 35 | ミニチュア | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 迷你模型 | Mini Model | 楽之介：家具のミニチュアもたくさんある！ |
| 36 | オルゴール | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 八音盒 | Music Box | 奏：（あとは——オルゴール） |
| 37 | アクアリウム | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 水族箱 | Aquarium | ミク：……これが、アクアリウム？ |
| 38 | 絵名の絵 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 绘名的画作 | Ena's Painting | ミク：これ全部、絵名の絵？　すごい…… |
| 39 | かわいい服 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 可爱的服装 | Cute Outfit | クロミ：（か、かわいい服に、ファッションショー！？） |
| 40 | ルカのベース | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 巡音流歌的贝斯 | Luka's Bass Guitar | ミク：……！　相変わらず、ルカのベースはカッコイイ……！ |
| 41 | コーヒーミル | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 咖啡研磨机 | Coffee Grinder | MEIKO：ふふ、コーヒーミルよ。いい豆も見つかったら、 |
| 42 | 『RAD WEEKEND』のフライヤー | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | “RAD WEEKEND”的传单 | RAD WEEKEND Flyer | 彰人：あの壁の『RAD WEEKEND』のフライヤー、 |
| 43 | グラフィティ | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 涂鸦 | Graffiti | 杏：そういえば、この壁のグラフィティアート、 |
| 44 | 志歩のベース | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 志步的贝斯 | Shiho's Bass Guitar | ルカ：志歩のベース、手入れが行き届いているわね |
| 45 | アイドルグッズ | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 偶像周边 | Idol Merch | それとも、アイドルグッズ用の衣装ケース？ |
| 46 | カフェの看板 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 咖啡厅的广告板 | Cafe Signboard | 絵名：『でしょ？　それに、カフェの看板メニューの |
| 47 | バーチャル・シンガー | 0.90 | 1 | organization | A | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | 虚拟歌手 | VIRTUAL SINGER | 彰人：バーチャル・シンガーとして歌ってるならまだしも、 |
| 48 | Leo/need | 0.90 | 1 | organization,other | A | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Leo/need | Leo/need | 咲希：え、うーん。Leo/needだし……。 |
| 49 | MORE MORE JUMP！ | 0.90 | 1 | organization,other | A | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | MORE MORE JUMP！ | MORE MORE JUMP! | みのり：そっか……いいなぁ、MORE MORE JUMP！として、 |
| 50 | Vivid BAD SQUAD | 0.90 | 1 | organization | A | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Vivid BAD SQUAD | Vivid BAD SQUAD | えむ：えへへ、あたしも『初めて見たVivid BAD SQUAD、 |
| 51 | ワンダーランズ×ショウタイム | 0.90 | 1 | organization | A | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | Wonderlands×Showtime | Wonderlands×Showtime | ワンダーランズ×ショウタイムだけじゃないってことだよ |
| 52 | 25時、ナイトコードで。 | 0.90 | 1 | organization | A | en,ja,ko,unitName,unitProfileName,zh_hans,zh_hant | 25点，Nightcord见。 | Nightcord at 25:00 | 『25時、ナイトコードで。のメンバー考察』の記事見た？ |
| 53 | 神山高校 | 0.90 | 1 | location | A | en,ja,ko,name,zh_hans,zh_hant | 神山高校 | Kamiyama High School | こはね：杏ちゃんも東雲くんも青柳くんも同じ神山高校なんだよね。 |
| 54 | ライブハウス | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | Live House | Live House | 志歩：あ、このラーメン屋、ライブハウスのお客さんが、 |
| 55 | ソロライブ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 个人演唱会 | Solo Live | ソロライブのリハーサルに出てるの |
| 56 | ファンフェスタ | 0.80 | 1 | event | C | en,ja,zh_hans | 粉丝节 | SINGER music | 一歌：あ、ファンフェスタのCMが流れてる。 |
| 57 | ワンマンライブ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 专场演唱会 | Solo Live | 咲希：絶対いるよ！　ワンマンライブの打ち上げだし、 |
| 58 | コンテスト | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 大赛 | Contest | 衣装コンテストがあるみたいだよ！ |
| 59 | シブヤ・フェスタ | 0.80 | 1 | event | C | ja,zh_tw |  |  | ミク：このあいだの、シブヤ・フェスタだったっけ。 |
| 60 | アークランドショーコンテスト | 0.80 | 1 | event,location | C | en,ja,ko,zh_hans,zh_tw | 弧光乐园表演大赛 | Ark Land Show Contes | 司：アークランドショーコンテストを通して、 |
| 61 | シブフェス | 0.80 | 1 | event | C | en,ja,zh_hans,zh_tw | 涩谷艺术节 | Shibuya Festa | 絵名：うん。もうすぐシブフェスも終わっちゃうけど…… |
| 62 | ドームライブ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 巨蛋演唱会 | Dome Live | ドームライブをやりたいんだ |
| 63 | ショーコンテスト | 0.80 | 1 | event | C | en,ja,zh_hans | 表演大赛 | Show Contest | 前にやったショーコンテストとは違うから |
| 64 | ブラフェス | 0.80 | 1 | event | C | en,ja,zh_hans,zh_tw | 婚庆展 | Bridal Festa | アタシもみんなとブラフェス行けて、配信出れたのにー！』 |
| 65 | ライブカフェ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | Live咖啡厅 | Live Cafe | 絵名：ねえ、入り浸ってるライブカフェがあるでしょ。 |
| 66 | ライブグッズ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 演出周边 | Live Goods | みのり：あ、ライブグッズのコーナーがあるよ！ |
| 67 | ミニライブ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 迷你演唱会 | Mini Live | 新人アイドルのミニライブやってたのか |
| 68 | ストリートライブ | 0.80 | 1 | event | C | en,ja,ko,zh_hans,zh_tw | 街头演出 | Street Live | 通行人達：お前らのストリートライブ、久しぶりじゃねえか！ |
| 69 | フェニックス・ブライダルフェスタ | 0.80 | 1 | event | C | en,ja,zh_hans,zh_tw | 凤凰婚庆展 | Phoenix Bridal Festa | 『フェニックス・ブライダルフェスタ』のチケットもらったの～ |
| 70 | ブライダルフェスタ | 0.80 | 1 | event | C | en,ja,zh_hans,zh_tw | 婚庆展 | Bridal Festa | リン：あっ、ブライダルフェスタの展示場で見たよ！ |
| 71 | ライブイベント | 0.80 | 1 | event | C | en,ja,zh_hans | 演出活动 | Live Event | みのり：この３人でライブイベント見に行くの久しぶりだね！ |
| 72 | フォトコンテスト | 0.80 | 1 | event | C | en,ja,zh_hans | 摄影大赛 | Photo Contest | どこからフォトコンテストのことが伝わるか |
| 73 | ジャムフェス | 0.80 | 1 | event | C | en,ja,zh_hans | 果酱节 | Jam Fest | 真堂：——そろそろ、ジャムフェスの準備をしなければいけませんので |
| 74 | ライブハウススタッフ | 0.80 | 1 | event | C | en,ja |  | Kimura | ライブハウススタッフA：おいおい、びっくりしてるだろ。 |
| 75 | カウントダウンライブ | 0.80 | 1 | event | C | en,ja |  | Countdown Show | 絵名：とか言って、今年もアニソンのカウントダウンライブに |
| 76 | フェニックスワンダーランド | 0.73 | 1 | location,product | C | en,ja,ko,zh_hans,zh_tw | 菲尼克斯奇幻乐园 | Phoenix Wonderland | えむ：穂波ちゃんはフェニックスワンダーランドで |
| 77 | ポップアップストア | 0.73 | 1 | product | C | en,ja,ko,zh_hans,zh_tw | 快闪店 | Pop-up Store | ポップアップストアができたんだよ！ |
| 78 | ライリードリームパーク | 0.73 | 1 | location,product | C | en,ja,ko,zh_hans,zh_tw | 莱利梦幻乐园 | Riley Dream Park | えむ：あ、ライリードリームパークの役者さん達に来てもらうとか！ |
| 79 | ネットパラダイス | 0.73 | 1 | product | C | en,ja,ko,zh_hans,zh_tw | 网络天堂 | NetParadise | 遥：これって、ネットパラダイスの番組？ |
| 80 | ワンダーステージ | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 奇幻舞台 | Wonder Stage | ワクワクわっしょいなワンダーステージへ！ |
| 81 | レコード | 0.69 | 1 | organization | C | en,ja,ko,zh_hans,zh_tw | 唱片 | Record | 杏：ねえ、レコードとか選ぶときどうしてる？ |
| 82 | メインステージ | 0.69 | 1 | location | C | ja,zh_hans | 主舞台 |  | 当たってもメインステージが遠い席だったけど…… |
| 83 | ワンダーランズ | 0.69 | 1 | organization | C | en,ja,ko,zh_hans,zh_tw | Wonderlands×Showtime | Wonderlands×Showtime | ワンダーランズ×ショウタイムだけじゃないってことだよ |
| 84 | フェニックスステージ | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 凤凰舞台 | Phoenix Stage | 類：そうだね。特にフェニックスステージとの |
| 85 | アークランド | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 弧光乐园 | Ark Land | 司：アークランドのワークショップのおかげで、 |
| 86 | プロダクション | 0.69 | 1 | organization | C | en,ja,ko,zh_hans,zh_tw | 制作公司 | Production | 音楽プロデューサーや芸能プロダクション…… |
| 87 | セカイ | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 世界 | SEKAI | レン：こんなセカイでやってるなら、カイト達のショーも |
| 88 | ブランド | 0.69 | 1 | location | C | en,ja |  | Fleur Libre | どこのだろう？　メジャーなブランドだったら、 |
| 89 | メリーゴーランド | 0.69 | 1 | location | C | en,ja |  | Smiley | レン：あはは。メリーゴーランドや汽車が空を飛んでるなんて、 |
| 90 | ショーステージ | 0.69 | 1 | location | C | en,ja |  | Tokyo ArcLand | 類：カイトさん、ショーステージの舞台機構を |
| 91 | ソリス・レコード | 0.69 | 1 | organization | C | en,ja |  | You're Leo | 咲希：よーし……いざ、ソリス・レコードへ！ |
| 92 | ライリー・エンターテインメント | 0.69 | 1 | organization | C | en,ja |  | Entertainment | 慶介：俺達は明日、ライリー・エンターテインメント社と、 |
| 93 | アニマルランド | 0.69 | 1 | location | C | en,ja |  | Odaiba Animal | 遥：昨日、みのりがアニマルランドに |
| 94 | お台場アニマルランド | 0.69 | 1 | location | C | en,ja |  | Odaiba Animal | 『お台場アニマルランド』って、たしか屋内動物園だったよね |
| 95 | デッサン | 0.49 | 1 | other | C | ja,zh_hans | 完成才行 |  | 穂波：はぁ……次の美術の授業、人物のデッサンか…… |
| 96 | クレープ | 0.49 | 1 | other | C | ja,zh_tw |  |  | 遥：クレープ、パンケーキに、流行りのスイーツ……。 |
| 97 | ネネロボ | 0.49 | 1 | other | C | en,ja,zh_hans,zh_tw | 世 界 | Robo-Nene malfunctio | 類：最近ネネロボの調子はどうだい？ |
| 98 | ナイトコード | 0.49 | 1 | other | C | en,ja,ko,zh_hans,zh_tw | Nightcord | Nightcord | じゃあ、またナイトコードで |
| 99 | リン・レン | 0.49 | 1 | other | C | en,ja,ko,zh_hans,zh_tw | 镜音铃·连 | Rin & Len | リン・レン：『ぷっ……あはは』 |
| 100 | ファミレス | 0.49 | 1 | other | C | ja,zh_hans | 多米诺骨牌 |  | 志歩：ねえ、そこのファミレスでご飯食べていかない？ |
| 101 | ニーゴ | 0.49 | 1 | other | C | en,ja,ko,zh_hans | 光源 | N25 Rangers | 瑞希：ニーゴとして発表した曲、結構増えてきたよね。 |
| 102 | フェニラン | 0.49 | 1 | other | C | en,ja,zh_hans | 嗷呜～！ | Kagami Mochi | これからフェニランに行くことってないんだよね……？ |
| 103 | プレゼン | 0.49 | 1 | other | C | ja,zh_hans | 宣讲 |  | 司くん達にもプレゼンしに行きましょ！ |
| 104 | ドレス | 0.49 | 1 | other | C | en,ja,zh_hans | 请多指教～！ | Th-The hem | で、前回優勝したのがエアバッグを使ったドレスでさ！ |
| 105 | チョコフォンデュ | 0.49 | 1 | other | C | ja,zh_tw |  |  | 愛莉：チョコフォンデュの次はチーズフォンデュってわけね。 |
| 106 | マックス | 0.49 | 1 | other | C | en,ja,zh_hans | 麦克思 | Max | 彰人：（……次こそ優勝しろよ。石原、マックス） |
| 107 | ニューヨーク | 0.49 | 1 | other | C | en,ja,zh_hans | 纽约 | Lloyd Hopper | こはね：ニューヨークでも、路上で練習してると、 |
| 108 | モグラ | 0.49 | 1 | other | C | ja,zh_tw |  |  | モグラのぐらぽんが、地下の動物さん達を守るためにがんばってて |
| 109 | スネア | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 小鼓 |  | レン：ほら、スネアとかバスドラムの大きさとか |
| 110 | コウテイペンギン | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 帝企鹅 |  | コウテイペンギンって、ペンギンの中では大きな種類だけど |
| 111 | アニマルカフェ | 0.49 | 1 | other | C | en,ja,zh_hans | 动物咖啡厅 | Flufftacular | 一歌：アニマルカフェに置くぬいぐるみや |
| 112 | タルト | 0.49 | 1 | other | C | ja,zh_hans | 超多 |  | 絵名：『ちょっと待ってて。たしか前に、アップルパイやタルトが |
| 113 | クリスマス | 0.49 | 1 | other | C | en,ja,ko,zh_hans,zh_tw | 个圣诞 | Okuyama | クリスマス特別メニューのハニーワッフルを食べる奴はいるか？ |
| 114 | カウントダウン | 0.49 | 1 | other | C | ja,zh_tw |  |  | それじゃあ、ライブ開始のカウントダウン——いくよ！』 |
| 115 | コンロ | 0.49 | 1 | other | C | ja,ko,zh_tw |  |  | ネネロボ：ハイ。携帯コンロ、鉄のローラー、ソシテ |
| 116 | マリオネット | 0.49 | 1 | other | C | en,ja,zh_hans | 提线木偶 | Marionette | 奏：マリオネット…… |
| 117 | ドミノ | 0.49 | 1 | other | C | ja,zh_hans | 多米诺骨牌 |  | 司：おお！　さては、『ドミノ倒し』か？ |
| 118 | ガラガラ | 0.49 | 1 | other | C | ja,zh_tw |  |  | 人数全然いないからガラガラで寂しいだけだし） |
| 119 | マシュマロ | 0.49 | 1 | other | C | en,ja,zh_hans | 超多 | Sweets Love | えむ：クッキーと、チョコレートと、マシュマロと、おせんべいと…… |
| 120 | リオン | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 里昂 |  | リオンくんと一緒に手を伸ばしてた時！ |
| 121 | クリスマスショー | 0.49 | 1 | other | C | en,ja,zh_tw |  | Hence | えむ：うん♪　えへへ、クリスマスショー楽しみだなあ |
| 122 | ランタン | 0.49 | 1 | other | C | en,ja,ko,zh_hans,zh_tw | 提灯 | Lanterns | 類：ああ。ゲレンデの雪とランタンの明かりが、 |
| 123 | トナカイ | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 雪橇 |  | それならトナカイ、雪だるま、サンタクロース…… |
| 124 | キラリ | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 闪耀星 |  | ひかりん：指と指をぴぴっとくっつけて、キラリ星のお約束！ |
| 125 | クロスワード | 0.49 | 1 | other | C | ja,zh_hans | 一道谜题 |  | 冬弥：『謎の答えをクロスワードに入れよ。 |
| 126 | カウントダウンパーティー | 0.49 | 1 | other | C | ja,zh_tw |  |  | 絵名：カウントダウンパーティーか…… |
| 127 | ガオガオ | 0.49 | 1 | other | C | ja,zh_tw |  |  | しばおモデルの『ガオガオてるてる坊主』のおかげだね！ |
| 128 | カメサン | 0.49 | 1 | other | C | ja,zh_tw |  |  | ぬいぐるみA：ウン！　アリガトウ、カメサン！ |
| 129 | クーン | 0.49 | 1 | other | C | ja,zh_tw |  |  | マックス：……クーン |
| 130 | アウトドアウェディング | 0.49 | 1 | other | C | ja,zh_hans | 逐一 |  | 水族館ウェディング、アウトドアウェディングなどがあり—— |
| 131 | ワンダショ | 0.49 | 1 | other | C | en,ja,zh_hans | 请多指教～！ | IPs | 『あれ？　もしかしてワンダショの子！？』 |
| 132 | マウスピース | 0.49 | 1 | other | C | ja,zh_hans | 吹嘴 |  | 一度マウスピースだけで練習してみるのがいいかもしれない |
| 133 | モアモアダンス | 0.49 | 1 | other | C | ja,zh_tw |  |  | 愛莉：ふふ、それじゃあ『みんなでモアモアダンス！』のコーナーは |
| 134 | メインバンド | 0.49 | 1 | other | C | ja,zh_hans | 跟一般 |  | 前座の仕事は、メインバンドの前に |
| 135 | 三国志 | 0.49 | 1 | other | C | en,ja,zh_hans | 作或 | Three Kingdoms | 先生：今回の応援合戦のテーマは『三国志』とのことです。 |
| 136 | ブラックギャラクシーソルジャー | 0.49 | 1 | other | C | en,ja,zh_hans | 暴击 | Black Galaxy Soldier | 寧々：（『ブラックギャラクシーソルジャー』を |
| 137 | スーパーデラックス | 0.49 | 1 | other | C | en,ja,zh_hans | 升降装置 | Ejection Mechanism | 類：僕が腕によりをかけて改造したスーパーデラックス射出装置を、 |
| 138 | ・リオ | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 少年里欧 |  | 少年・リオ役のオーディションを受けることとなったわけだ！！ |
| 139 | マッドサイエンティスト | 0.49 | 1 | other | C | en,ja,zh_tw |  | Explosive Mad Scient | 『爆破のマッドサイエンティスト』だったから、 |
| 140 | トーヤ | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 罗伊德先生…… |  | エレナ：セディがトーヤを呼びにいくっていうから、ついてきたの！ |
| 141 | 寧々ちゃん…… | 0.49 | 1 | other | C | ja,zh_hans | 小宁宁…… |  | MEIKO：『寧々ちゃん……』 |
| 142 | 美味しい～！ | 0.49 | 1 | other | C | ja,zh_hans | 好吃～！ |  | 『美味しい～！』っていう気持ちで |
| 143 | プロポーズ | 0.49 | 1 | other | C | ja,zh_hans | 求婚 |  | 彰人：何がって、プロポーズする面子だよ。 |
| 144 | ハルミチ | 0.49 | 1 | other | C | ja,zh_tw |  |  | 秋志：『彼らに会うたびに“ハルミチは日本で何をしてるんだ？” |
| 145 | サモちゃん？ | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 小萨？ |  | 遥：『サモちゃん？』 |
| 146 | ゴール | 0.49 | 1 | other | C | en,ja,zh_hans | 扮演大灰狼 | Gowan | ボールがバーン！ってゴールに入るんだよ！ |
| 147 | アロマキャンドル | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 香薰蜡烛 |  | アロマキャンドルの香りかも |
| 148 | ムース | 0.49 | 1 | other | C | ja,zh_hans | 慕斯 |  | こういうムースのケーキまで作れちゃうのね |
| 149 | クランクアップ | 0.49 | 1 | other | C | ja,zh_tw |  |  | えむ：あたし達も、クランクアップってやつだね☆ |
| 150 | リアイベ | 0.49 | 1 | other | C | ja,zh_hans | 好羡慕～！ |  | コメント：『前のリアイベにスタッフで参加した人！？』 |
| 151 | 生き様 | 0.49 | 1 | other | C | en,ja,zh_tw |  | NetParadise | 有澤：だから私は、“生き様”で希望を届けられるのが |
| 152 | ヘッショ | 0.49 | 1 | other | C | en,ja,zh_hans | 暴击 | Black Galaxy Soldier | やれたのはよかったな、ヘッショも結構決められたし。 |
| 153 | キャンプ | 0.49 | 1 | other | C | ja,zh_hans | 色再 |  | 山でキャンプしたり、海に行って遊んだりしたいでーす！ |
| 154 | ピーマン | 0.49 | 1 | other | C | ja,zh_hans | 青椒 |  | 鶏そぼろと炒り卵、それから炒めたピーマンだったわね |
| 155 | カッコイイ | 0.49 | 1 | other | C | en,ja,zh_hans | 兼具 | You're Arata | ライバルの騎士に向かって言うセリフ、カッコイイ！ |
| 156 | ソーセージ | 0.49 | 1 | other | C | en,ja,zh_tw |  | Curveball Competitio | 愛莉：魚肉ソーセージとチョコって、意外と合うのね。 |
| 157 | シマショウ | 0.49 | 1 | other | C | ja,zh_hans | 世 界 |  | ネネロボ：ハイ。新タナセカイをミンナで探索シマショウ |
| 158 | ブレスレット | 0.49 | 1 | other | C | en,ja,zh_hans | 手环好 | Bracelets | 瑞希がブレスレットの手作りキット買ったお店なんだって |
| 159 | チョコペン | 0.49 | 1 | other | C | ja,zh_hans | 巧克力笔 |  | えむ：色の違うチョコペンを混ぜて、 |
| 160 | アイライン | 0.49 | 1 | other | C | ja,zh_hans,zh_tw | 眼线 |  | 寧々：うっ……アイライン、またズレた…… |
| 161 | セット | 0.49 | 1 | other | C | en,ja |  | Th-They're having | 髪セットする時間あんまりなかったな…… |
| 162 | チョコレート | 0.49 | 1 | other | C | en,ja |  | Th-Three truckloads | 穂波：えっ！　“アップルパイにチョコレート入れちゃいました”？ |
| 163 | ウサギ | 0.49 | 1 | other | C | en,ja |  | Headshot | ウサギさん達、元気かな |
| 164 | ゲームセンター | 0.49 | 1 | other | C | en,ja |  | PenPyon plushies | ゲームセンターにでも…… |
| 165 | アレンジ | 0.49 | 1 | other | C | en,ja |  | Arrange | 洋服のアレンジとかにも使えそう！ |
| 166 | サイン | 0.49 | 1 | other | C | en,ja |  | Night-capade | みのり：あっ、あの、愛莉ちゃん……さ、サインとかって…… |
| 167 | アトラクション | 0.49 | 1 | other | C | en,ja |  | Coaster | みのり：楽しそうなアトラクションがたくさんあるよ！ |
| 168 | バラエティ | 0.49 | 1 | other | C | en,ja,ko |  | Funtime Lunchtime | 昔、バラエティ番組の企画で来たことあるわ |
| 169 | テレビ | 0.49 | 1 | other | C | en,ja |  | TV Station | 咲希：あの！　アタシ、先輩のことよくテレビで見てて……！ |
| 170 | オリジナル | 0.49 | 1 | other | C | en,ja |  | Goodbye Ghosts | 司：寧々！　この前の金曜オリジナル特番は見たか？ |
| 171 | ビビッドストリート | 0.49 | 1 | other | C | en,ja |  | Vivid Street | こはね：あの……東雲くん達は、ビビッドストリートみたいな場所で |
| 172 | クラシック | 0.49 | 1 | other | C | en,ja |  | RADder putting | クラシックに合わせてラップしてる！ |
| 173 | キャラ | 0.49 | 1 | other | C | en,ja |  | Lazy Hippo | クラスの子には、熱射病になったゆるキャラって言われたのに！ |
| 174 | スカート | 0.49 | 1 | other | C | en,ja |  | Hiss | ほら、あのふわっとしたスカートとか、似合いそうなのに |
| 175 | チーズケーキ | 0.49 | 1 | other | C | en,ja |  | Cheesecake | 絵名：ふふ。ねえ、ほら見て。昨日、食べに行ったチーズケーキ！ |
| 176 | ゲーセン | 0.49 | 1 | other | C | en,ja |  | Th-They never | 咲希：ふんふ～ん♪　はるかちゃんと、ゲーセンだ♪ |
| 177 | フェニックス | 0.49 | 1 | other | C | en,ja |  | Phoenix Show | えむ：違うよ、フェニックスだよ！ |
| 178 | カード | 0.49 | 1 | other | C | en,ja |  | Kizaki | 杏：そうそう！　私はジャケットの写真とか歌詞カードとかも |
| 179 | トッピング | 0.49 | 1 | other | C | en,ja |  | Chocotas | こはね：私、ついいろいろトッピングお願いしちゃうけど、 |
| 180 | ボタン | 0.49 | 1 | other | C | en,ja |  | End Stream | 咲希：えーっと、このボタンを押しながらここを回して……。 |
| 181 | ジャズ | 0.49 | 1 | other | C | en,ja |  | Lloyd Hopper | レン：うーん……例えばジャズとかさ。 |
| 182 | クリエイター | 0.49 | 1 | other | C | en,ja,ko |  | VIRTUAL SINGERs | はい、このクリエイターさんの曲を聴くことが多いよ |
| 183 | データ | 0.49 | 1 | other | C | en,ja |  | Alfred | 類：それから無数のデータを共有できたり |
| 184 | コンセプト | 0.49 | 1 | other | C | en,ja |  | Afternoon Girls | 瑞希：バンドのコンセプトが |
| 185 | プラン | 0.49 | 1 | other | C | en,ja |  | See Shows | そのプラン試してみたいね |
| 186 | インタビュー | 0.49 | 1 | other | C | en,ja |  | REAL STAGE | インタビュー記事とかもあって面白いよ |
| 187 | サイズ | 0.49 | 1 | other | C | en,ja |  | Th-These guests | 微妙にサイズが合わないこともあるから、 |
| 188 | スペシャル | 0.49 | 1 | other | C | en,ja |  | Wafty-wafty-waft | スペシャルなアレンジもしてるんだ～♪ |
| 189 | ハーブ | 0.49 | 1 | other | C | en,ja |  | Thyme | KAITO：ん？　ふたりからハーブみたいな香りがするね |
| 190 | ドラゴン | 0.49 | 1 | other | C | ja,ko |  |  | ドラゴンの頭にぐさーって刺さって…… |
| 191 | パジャマ | 0.49 | 1 | other | C | en,ja |  | PJs today | 頭が３つあるワンチャンのパジャマまだ売ってた！ |
| 192 | チャレンジ | 0.49 | 1 | other | C | en,ja |  | Okamura | ボク、ちょっとチャレンジしてみちゃおうかな？ |
| 193 | バーン | 0.49 | 1 | other | C | en,ja |  | Hood | あそこのおっきなモニタにバーンって出て—— |
| 194 | メンテナンス | 0.49 | 1 | other | C | en,ja |  | GRR | メンテナンスしてもらえないかなっ？ |
| 195 | ファッションショー | 0.49 | 1 | other | C | en,ja |  | Phoenix Bridal Fair | ファッションショーしようよ！　絶対楽しいって！ |
| 196 | ドキュメンタリー | 0.49 | 1 | other | C | en,ja |  | Funtime Lunchtime, | トーク番組とか、バラエティとか、ドキュメンタリーとか…… |
| 197 | ベテラン | 0.49 | 1 | other | C | en,ja,ko |  | Conceptually | KAITO：え？　それは、メイコさんがベテランのアイドルだからだよ |
| 198 | アクロバティック | 0.49 | 1 | other | C | en,ja |  | KAITO once | KAITO：なるほど……アクロバティックな動きを入れたいんだね |
| 199 | モモジャン | 0.49 | 1 | other | C | en,ja |  | Nanamin Channel | モモジャンが伝説的なアイドルになってるんじゃない？ |
| 200 | ロープ | 0.49 | 1 | other | C | en,ja |  | Star Training Suit | 愛莉：ええ！　蔦でロープを作りたいんだけど、 |
| 201 | ピース | 0.49 | 1 | other | C | en,ja |  | WEEKEND meant | 100ピースでも食べきれないのに！？ |
| 202 | ピンチ | 0.49 | 1 | other | C | en,ja |  | Last | ピンチに駆けつける！的な登場の仕方をしたいわね！ |
| 203 | ハート | 0.49 | 1 | other | C | en,ja |  | Blesse | アイドルハート：重なるハートが奇跡を起こす！ |
| 204 | キャッチ | 0.49 | 1 | other | C | en,ja |  | Caw | 杏：く～っ、葉っぱキャッチ対決は引き分けか！ |
| 205 | フォロワー | 0.49 | 1 | other | C | en,ja |  | Busy | ちょっとずつフォロワー増えてるよ！ |
| 206 | クール | 0.49 | 1 | other | C | en,ja |  | Original Miku Songs | 『どうぞ……』ってクールに渡す感じ！？ |
| 207 | ジョギング | 0.49 | 1 | other | C | en,ja |  | Exercise | 企画は朝、いつものジョギングしてる時に思いついたの |
| 208 | スタンプ | 0.49 | 1 | other | C | en,ja |  | Operation Payback | あとはニコニコ笑顔のスタンプっと…… |
| 209 | リアルイベント | 0.49 | 1 | other | C | en,ja |  | Ayaka | みのり：うん！　リアルイベントやってからずっと、 |
| 210 | チアデ | 0.49 | 1 | other | C | en,ja |  | Days MCs | 愛莉：朝テレビ見てたら、たまたまチアデが出てたのよ。 |
| 211 | アイハザ | 0.49 | 1 | other | C | en,ja,ko |  | Marina | 昨日、わたし達が出たアイハザを一緒に見てたんだけど—— |
| 212 | アリサ | 0.49 | 1 | other | C | en,ja,ko |  | Higure | みのり：あっ、あとね！　アリサちゃんの質問に |
| 213 | ナイトショー | 0.49 | 1 | other | C | en,ja |  | 30th Anniversary Sho | 類：ナイトショーがSNSで話題になってから、 |
| 214 | レンガセンター | 0.49 | 1 | other | C | en,ja |  | Red Brick Center | 遥：じゃあ私からも、赤レンガセンターで買ったお菓子。 |
| 215 | ファンレター | 0.49 | 1 | other | C | en,ja |  | Dear MMJ | ファンレターを送ろうとした時のことで、 |
| 216 | フォーム | 0.49 | 1 | other | C | en,ja |  | Skip | まずは正しいフォームで、１回やってみるところから始めましょ！ |
| 217 | エリア | 0.49 | 1 | other | C | en,ja |  | Area | 回れなかったエリアもたくさんあるし…… |
| 218 | ランキング | 0.49 | 1 | other | C | en,ja |  | ID somewhere | ランキングにも名前がのるくらいだもんな |
| 219 | レーベル | 0.49 | 1 | other | C | en,ja |  | You're Leo | RADderの実力は、海外レーベルとも契約を結べるほどだ。 |
| 220 | セドリック | 0.49 | 1 | other | C | en,ja |  | SONIC | 杏：スレイドとセドリックの秘密基地、カッコよかったね〜！ |
| 221 | アンドロイド | 0.49 | 1 | other | C | en,ja |  | Professor Christophe | アンドロイド役のクセが残ってしまっているのかもしれん |
| 222 | システム | 0.49 | 1 | other | C | en,ja |  | Robo-Nene OS | 奏：……そ、そういう便利なシステムじゃないんじゃないかな…… |
| 223 | バートレット | 0.49 | 1 | other | C | en,ja |  | Intruders | バートレットから声をかけに行くシーンだ。 |
| 224 | アルフレッド | 0.49 | 1 | other | C | en,ja |  | Alfred | アルフレッドも司くんと旭さんで違うから |
| 225 | エース | 0.49 | 1 | other | C | en,ja |  | Ace | はい、エースのスリーペアよ |
| 226 | サツマイモ | 0.49 | 1 | other | C | en,ja |  | Curveball Competitio | なんとサツマイモまで入ってるんだよ！ |
| 227 | イルカ | 0.49 | 1 | other | C | en,ja |  | Blue World | 絵名：（魚……人魚、イルカ……） |
| 228 | ドーナツ | 0.49 | 1 | other | C | en,ja |  | Newest Donuts | ドーナツの中からわんちゃんとかうさちゃんが顔出してる！ |
| 229 | ウィーン | 0.49 | 1 | other | C | en,ja |  | Australia | こう、クリーナーでウィーン！ってやるのも楽しいんだよね～♪ |
| 230 | ニュース | 0.49 | 1 | other | C | en,ja |  | Perseid | KAITO：その様子だと、いいニュースみたいだね！ |
| 231 | ローラースケート | 0.49 | 1 | other | C | en,ja |  | Rollerblading | ローラースケート、おもしろかったね～！ |
| 232 | ロミオ | 0.49 | 1 | other | C | en,ja |  | Sweet Juliet | 瑞希：うん！　ロミオの劇見て、 |
| 233 | イチゴ | 0.49 | 1 | other | C | en,ja |  | Strawberries | みのり：生クリームの上にイチゴが、 |
| 234 | ユイカ | 0.49 | 1 | other | C | en,ja |  | Cool Stuff | ユイカ：うっ、演技で初見っぽい反応しなくちゃいけないってことか……。 |
| 235 | 銀之助捕物帳 | 0.49 | 1 | other | C | en,ja |  | Edo-period | 次の公演『銀之助捕物帳』の配役が決まってしまうのだからね |
| 236 | ツインベース | 0.49 | 1 | other | C | en,ja |  | Rookie Stage | レン：ツインベースってやつだな！ |
| 237 | チョコケーキ | 0.49 | 1 | other | C | en,ja |  | Considering Saki | チョコケーキも捨てがたいし…… |
| 238 | ドルラバ | 0.49 | 1 | other | C | en,ja |  | Idol Lovers Festival | 愛莉：（昨日、ドルラバに出演できるって話を聞いてから、 |
| 239 | アイドルラバーズ | 0.49 | 1 | other | C | en,ja |  | Idol Lovers Festival | 実は……私達、アイドルラバーズからオファーがきたんです！ |
| 240 | タイアップ | 0.49 | 1 | other | C | en,ja |  | Cluster | ミク：『今やってるのは、例のタイアップ曲？』 |
| 241 | デリサイダー | 0.49 | 1 | other | C | en,ja |  | Lemonade-flavored Fi | しゅわデリサイダーは絶対持って行こうよ！ |
| 242 | ワンワンッ | 0.49 | 1 | other | C | en,ja |  | Marron | サモちゃん：ワンワンッ！ |
| 243 | リュカ | 0.49 | 1 | other | C | en,ja |  | Luca | まだリュカへの理解が甘いこともわかってたんだ |
| 244 | ロイド | 0.49 | 1 | other | C | en,ja |  | Hopper | 冬弥：……そろそろロイドさんが教えてくれた校内の |
| 245 | ラインストーン | 0.49 | 1 | other | C | en,ja |  | Rhinestones | ラインストーンで星空を作ってみたんだ♪ |
| 246 | プリンセス | 0.49 | 1 | other | C | en,ja |  | Cursed Flower | 寧々：うん。プリンセスの演目を手掛けてる演出家さんの本なんだ |
| 247 | マット | 0.49 | 1 | other | C | en,ja |  | Augh | 類：それに、司くんの提案で練習用のマットも増えて、 |
| 248 | ドイツ | 0.49 | 1 | other | C | en,ja |  | German | 彰人：ズーストルテっつーと、ドイツの有名なとこか。 |
| 249 | シャオ | 0.49 | 1 | other | C | en,ja |  | Windbag | リンの声：『——こうして、シャオ達の魔法は、 |
| 250 | ・ハッピーエブリデイ | 0.49 | 1 | other | C | en,ja |  | Masked Variety | バラエティアイドル仮面・ハッピーエブリデイが来てくれてね |
| 251 | ワクワクゲーム | 0.49 | 1 | other | C | en,ja |  | Gaming Tournament | 冬弥：すみません、チラシにあった『ワクワクゲーム大会』に |
| 252 | バナナボート | 0.49 | 1 | other | C | en,ja |  | Wh-What we're | みのり：（どんな特訓かな……！　きっとバナナボートみたいに |
| 253 | 暗黒料理人会 | 0.49 | 1 | other | C | en,ja |  | Dark Culinary Societ | 『暗黒料理人会』に所属する料理人達を使い食料を独占し、 |
| 254 | ——待って！ | 0.49 | 1 | other | C | en,ja |  | *Sigh* | 少女：『——待って！』 |
| 255 | 草原地帯エリア | 0.49 | 1 | other | C | en,ja |  | Grassland Area | 『草原地帯エリア』の担当だって！ |
| 256 | サバンナエリア | 0.49 | 1 | other | C | en,ja |  | Savannah | わたし、『サバンナエリア』が結構気になってるんだ！ |
| 257 | ショパン | 0.49 | 1 | other | C | en,ja |  | Fantaisie-Impromptu | 冬弥：これは、ショパンの幻想即興曲の楽譜ですね |
| 258 | ワオーン | 0.49 | 1 | other | C | en,ja |  | Howl | 犬ロボ：ワオーン！ |
| 259 | ピアニスト | 0.49 | 1 | other | C | en,ja |  | Pianist | 冬弥：例えば、クラシック音楽を専門に学んだピアニストが |
| 260 | ブライダルイベント | 0.49 | 1 | other | C | en,ja |  | Wedding Designed | テーマとしたブライダルイベントです！ |
| 261 | ハッピーウェディングショー | 0.49 | 1 | other | C | en,ja |  | Wedding Show | みのり：寧々ちゃんは夕方から始まる『ハッピーウェディングショー』の |
| 262 | リュカ・ルーセル | 0.49 | 1 | other | C | en,ja |  | Luca Roussel | リュカ・ルーセル） |
| 263 | ルイーズ | 0.49 | 1 | other | C | en,ja |  | Louise | 王女ルイーズは小宮山さん、お願いします |
| 264 | トウヤ | 0.49 | 1 | other | C | en,ja |  | Hey, Toya. | ？？？：『——————おい、トウヤ』 |
| 265 | グッバイ・ゴースト | 0.49 | 1 | other | C | en,ja |  | Goodbye Ghosts' | 類：（——ショーの演目は、『グッバイ・ゴースト』） |
| 266 | パスワード | 0.49 | 1 | other | C | ja,ko |  |  | 何度も正しいパスワードを入力しているのですが、どうやら |
| 267 | 世界ワッハッハTV | 0.49 | 1 | other | C | en,ja |  | Masked Variety | “世界ワッハッハTV”が大好きでした！』 |
| 268 | レンレン | 0.49 | 1 | other | C | en,ja |  | Len-Len | レン：『じゃあボクも！　飛び出てレンレン♪』 |
| 269 | サバンナ | 0.49 | 1 | other | C | en,ja |  | Savannah | 類：前回は『サバンナ』がテーマで |
| 270 | レミドール | 0.49 | 1 | other | C | en,ja |  | Shinono-no-me | レミドールの『パレード』！？ |
| 271 | ボランティアスタッフ | 0.49 | 1 | other | C | en,ja |  | Information | ボランティアスタッフ：あ……！　すみません！ |
| 272 | アルフレッド・ホーキング | 0.49 | 1 | other | C | en,ja |  | Hawking | アルフレッド・ホーキングという』 |
| 273 | センチュリーホール | 0.49 | 1 | other | C | en,ja |  | Tokyo Century | なんとこの度——東京センチュリーホールにて、 |
| 274 | マスコット | 0.49 | 1 | other | C | en,ja |  | Emperor Penguin | 志歩：いつ見ても、マスコットのフェニーくんはかわいい |
| 275 | ブラック | 0.49 | 1 | other | C | en,ja |  | Doll Ranger | レン：よし！　今日はブラックを頼むぞ！ |
| 276 | アピール | 0.49 | 1 | other | C | en,ja |  | Appealing Talk Show | 司：（えむが、見え見えの困ったアピールをしている……） |
| 277 | ドラマ | 0.49 | 1 | other | C | en,ja |  | TV dramas | 今度、ドラマ化されるようだよ |
| 278 | オープン | 0.49 | 1 | other | C | en,ja |  | Kizaki | そこのオープン記念とかで、ここらのミュージシャン集めて |
| 279 | ヒール | 0.49 | 1 | other | C | en,ja |  | Events | ミク：うん。次のステージで、ちょっとヒールの高めのくつを |
| 280 | トライアングル | 0.49 | 1 | other | C | en,ja |  | Th-Those might | ミク：ねぇ、ルカ。演奏の時、たまに使ってるトライアングルとか、 |
| 281 | トランポリン | 0.49 | 1 | other | C | en,ja |  | Dreamy Night | えむ：じゃあ、トランポリン使って練習する？ |
| 282 | ペガサス | 0.49 | 1 | other | C | en,ja |  | Pegasus Attack | 司：……天翔けるペガサスと書き、天馬！ |
| 283 | アイドルスマイル | 0.49 | 1 | other | C | en,ja |  | You're Ena | まふゆ：すごい……。これが本物のアイドルスマイルなんだね |
| 284 | コメディ | 0.49 | 1 | other | C | en,ja |  | Comedy | コメディ映画をおすすめしたいな |
| 285 | チーズ | 0.49 | 1 | other | C | en,ja |  | Cheesecake | ベーコンとひと口サイズのチーズも買ったし大丈夫 |
| 286 | フェニックスコースター | 0.49 | 1 | other | C | en,ja |  | Coaster | 右手に見えるのがフェニックスコースターです！ |
| 287 | ファッションセンス | 0.49 | 1 | other | C | en,ja |  | Rina | 弟くんのファッションセンスを頼らせてよ |
| 288 | 好き | 0.49 | 1 | other | C | en,ja |  | Rina | みのり：好きなことを『好き』ってどどーんと胸張って言えるように、 |
| 289 | ボート | 0.49 | 1 | other | C | en,ja |  | Wh-What we're | ボートで下っていく夢なんだけど、 |
| 290 | クリスマスツリー | 0.49 | 1 | other | C | en,ja |  | Th-This Christmas | 100メートルのクリスマスツリーが欲しいって言ったこと？ |
| 291 | リフティング | 0.49 | 1 | other | C | en,ja |  | Musashino East | レン：サッカーっていうか、リフティングだよ。 |
| 292 | マジパン | 0.49 | 1 | other | C | en,ja |  | Santa Clause | マジパンのペンギンは、全部はるかちゃんにあげるね！ |
| 293 | キャンディ | 0.49 | 1 | other | C | en,ja |  | Sweets Love | 寧々：……えっと、キャンディみたいだけど？ |
| 294 | ペルセウス | 0.49 | 1 | other | C | en,ja |  | Perseid | ではなく、ペルセウス座流星群！ |
| 295 | モタモタ | 0.49 | 1 | other | C | en,ja |  | Having Tsukasa | 寧々：（ふう、家でモタモタしてたら遅刻ギリギリになっちゃった。 |
| 296 | ——♪　————♪ | 0.49 | 1 | other | C | en,ja |  | Amy | 一歌の声：『——♪　————♪』 |
| 297 | ゲレンデ | 0.49 | 1 | other | C | en,ja |  | Santa Clauses | 類：ああ。ゲレンデの雪とランタンの明かりが、 |
| 298 | カート | 0.49 | 1 | other | C | en,ja |  | Dreamy Night | で、置いてあったカートの大きさにもびっくりしてさ！ |
| 299 | ハッピーバレンタイン | 0.49 | 1 | other | C | en,ja |  | Tadaa | レン：奥のみんなにもハッピーバレンタイン！ |
| 300 | アフタヌーンガールズ | 0.49 | 1 | other | C | en,ja |  | Afternoon | 遥：（あ、これ。アフタヌーンガールズを |

## 待复核：高频 other（可能漏贴 tag）

| canonical | weight | occ | tags | evidence |
|-----------|--------|-----|------|----------|
