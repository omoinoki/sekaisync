# 用语权重总表

> 生成自 `store/kb/terms.json`（`--all --local --source-language ja` 全量抽取）。
> `weight = log1p(occurrences) * trust_factor * tag_prior`
> `trust: A 1.3 / B 1.1 / C 1.0 / D 0.9`，`tag_prior: person 1.2 / event 1.15 / product 1.05 / location 1.0 / organization 1.0 / other 0.7`（重叠取 max）。

## 概览

- 全量用语：9292 条（官方 105，带证据 9240）
- 仅日语（无他服译名，含 JP 领先期新增名词）：8771 条（94.4%）
- 多语对照：521 条
- 各语种覆盖：ja 9292, zh_hans 191, en 448, zh_tw 75, ko 143
- 各 tag 计数：event 108, location 180, organization 33, other 8933, person 26, product 29
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
| other | ケーキ | 0.49 | 1 | en,ja,zh_hans | 超多 / Strawberries |
| person | 星乃一歌 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 星乃一歌 / HOSHINO ICHIKA |
| person | 白石杏 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 白石杏 / SHIRAISHI AN |
| person | 東雲彰人 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 东云彰人 / SHINONOME AKITO |
| person | 青柳冬弥 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 青柳冬弥 / AOYAGI TOYA |
| person | 天馬司 | 1.08 | 1 | en,firstName,firstNameEnglish,full,givenName,givenNameEnglish,ja,ko,zh_hans,zh_hant | 天马司 / TENMA TSUKASA |
| product | 鏡音リン・鏡音レン　記念日ライブ配信 | 0.80 | 1 | ja-only |  /  |
| product | 初音ミク　記念日特別ライブ配信・予告動画 | 0.80 | 1 | ja-only |  /  |
| product | フェニックスワンダーランド | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 菲尼克斯奇幻乐园 / Phoenix Wonderland |
| product | ポップアップストア | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 快闪店 / Pop-up Store |
| product | ライリードリームパーク | 0.73 | 1 | en,ja,ko,zh_hans,zh_tw | 莱利梦幻乐园 / Riley Dream Park |

## 仅日语（only-ja）权重前 20

| # | canonical | weight | occ | tags | evidence |
|---|-----------|--------|-----|------|----------|
| 1 | 第１回神山高校芸術祭 | 0.80 | 1 | event,location | 司会：ただいまより『第１回神山高校芸術祭』、開会式を行います。 |
| 2 | サプライズライブ | 0.80 | 1 | event | サプライズライブをしようって話をしてて—— |
| 3 | バレンタインライブ | 0.80 | 1 | event | KAITO：バレンタインライブをすることは決めたけど、 |
| 4 | シブヤ夏祭り | 0.80 | 1 | event | こはね：『シブヤ夏祭り』っていうお祭りでやってたライブイベントが、 |
| 5 | バーチャル・シンガーファンフェスタ | 0.80 | 1 | event | 『バーチャル・シンガーファンフェスタ』告知PVなんだ |
| 6 | デザインコンテスト | 0.80 | 1 | event | きっと衣装デザインコンテストをやったからだね |
| 7 | 目指せドームライブ！ | 0.80 | 1 | event | 『目指せドームライブ！』だね！ |
| 8 | 神山高校芸術祭 | 0.80 | 1 | event,location | 先生：実は、今年から『神山高校芸術祭』という行事を |
| 9 | ジャムフェス・ルーキーステージ | 0.80 | 1 | event,location | ジャムフェス・ルーキーステージ、お次は——！』 |
| 10 | ライブ？ | 0.80 | 1 | event | 女の子の影：『ライブ？』 |
| 11 | ライブスペース | 0.80 | 1 | event | ライブスペースを見てたみたいだけど、どうしたの？ |
| 12 | ドライブ | 0.80 | 1 | event | 昔からよくドライブ連れてってくれたよね |
| 13 | ライブチケット | 0.80 | 1 | event | 志歩：申し込んでたライブチケットが当たったんだ |
| 14 | スクールバンドフェス | 0.80 | 1 | event | 穂波：このバンド、高校生の頃に『スクールバンドフェス』って |
| 15 | ハッピーウェディングライブ | 0.80 | 1 | event | ——ハッピーウェディングライブの始まりだよ！』 |
| 16 | シブヤ区高等学校合同文化祭 | 0.80 | 1 | event | 『シブヤ区高等学校合同文化祭』って言ってね |
| 17 | シブ学祭 | 0.80 | 1 | event | 長いから『シブ学祭』って呼ぼ！ |
| 18 | 春ライブ | 0.80 | 1 | event | “春ライブ”になるのかな？ |
| 19 | リリースライブ | 0.80 | 1 | event | 愛莉：ファーストシングルのリリースライブでね。 |
| 20 | ライブパフォーマンス | 0.80 | 1 | event | マスコットの人のライブパフォーマンスもすごいよね |

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
| 74 | 第１回神山高校芸術祭 | 0.80 | 1 | event,location | C | ja-only |  |  | 司会：ただいまより『第１回神山高校芸術祭』、開会式を行います。 |
| 75 | サプライズライブ | 0.80 | 1 | event | C | ja-only |  |  | サプライズライブをしようって話をしてて—— |
| 76 | バレンタインライブ | 0.80 | 1 | event | C | ja-only |  |  | KAITO：バレンタインライブをすることは決めたけど、 |
| 77 | シブヤ夏祭り | 0.80 | 1 | event | C | ja-only |  |  | こはね：『シブヤ夏祭り』っていうお祭りでやってたライブイベントが、 |
| 78 | バーチャル・シンガーファンフェスタ | 0.80 | 1 | event | C | ja-only |  |  | 『バーチャル・シンガーファンフェスタ』告知PVなんだ |
| 79 | デザインコンテスト | 0.80 | 1 | event | C | ja-only |  |  | きっと衣装デザインコンテストをやったからだね |
| 80 | 目指せドームライブ！ | 0.80 | 1 | event | C | ja-only |  |  | 『目指せドームライブ！』だね！ |
| 81 | 神山高校芸術祭 | 0.80 | 1 | event,location | C | ja-only |  |  | 先生：実は、今年から『神山高校芸術祭』という行事を |
| 82 | ライブハウススタッフ | 0.80 | 1 | event | C | en,ja |  | Kimura | ライブハウススタッフA：おいおい、びっくりしてるだろ。 |
| 83 | ジャムフェス・ルーキーステージ | 0.80 | 1 | event,location | C | ja-only |  |  | ジャムフェス・ルーキーステージ、お次は——！』 |
| 84 | ライブ？ | 0.80 | 1 | event | C | ja-only |  |  | 女の子の影：『ライブ？』 |
| 85 | カウントダウンライブ | 0.80 | 1 | event | C | en,ja |  | Countdown Show | 絵名：とか言って、今年もアニソンのカウントダウンライブに |
| 86 | ライブスペース | 0.80 | 1 | event | C | ja-only |  |  | ライブスペースを見てたみたいだけど、どうしたの？ |
| 87 | ドライブ | 0.80 | 1 | event | C | ja-only |  |  | 昔からよくドライブ連れてってくれたよね |
| 88 | ライブチケット | 0.80 | 1 | event | C | ja-only |  |  | 志歩：申し込んでたライブチケットが当たったんだ |
| 89 | スクールバンドフェス | 0.80 | 1 | event | C | ja-only |  |  | 穂波：このバンド、高校生の頃に『スクールバンドフェス』って |
| 90 | ハッピーウェディングライブ | 0.80 | 1 | event | C | ja-only |  |  | ——ハッピーウェディングライブの始まりだよ！』 |
| 91 | シブヤ区高等学校合同文化祭 | 0.80 | 1 | event | C | ja-only |  |  | 『シブヤ区高等学校合同文化祭』って言ってね |
| 92 | シブ学祭 | 0.80 | 1 | event | C | ja-only |  |  | 長いから『シブ学祭』って呼ぼ！ |
| 93 | 春ライブ | 0.80 | 1 | event | C | ja-only |  |  | “春ライブ”になるのかな？ |
| 94 | リリースライブ | 0.80 | 1 | event | C | ja-only |  |  | 愛莉：ファーストシングルのリリースライブでね。 |
| 95 | ライブパフォーマンス | 0.80 | 1 | event | C | ja-only |  |  | マスコットの人のライブパフォーマンスもすごいよね |
| 96 | ゲリラライブ | 0.80 | 1 | event | C | ja-only |  |  | この近くの通りでゲリラライブしてたらしいよ |
| 97 | ライブツアー | 0.80 | 1 | event | C | ja-only |  |  | MEIKO：ふふっ。ひとりライブツアーみたいな感じね！ |
| 98 | フラワーフェス | 0.80 | 1 | event | C | ja-only |  |  | みのり：見て見て、遥ちゃん！　『フラワーフェス』だって！ |
| 99 | トレードライブ | 0.80 | 1 | event | C | ja-only |  |  | トレードライブ、すっごくよかったって言ってくれたんだ！ |
| 100 | チームライブ | 0.80 | 1 | event | C | ja-only |  |  | 出張したチームライブの時の映像かしら？ |
| 101 | ブライダルライブ | 0.80 | 1 | event | C | ja-only |  |  | KAITO：ありがとう！　ブライダルライブがもっと華やかになりそうだよ！ |
| 102 | オープニングライブ | 0.80 | 1 | event | C | ja-only |  |  | 志歩：でも、オープニングライブのあと、 |
| 103 | ルカルカ☆イントロクイズ大会～っ♪ | 0.80 | 1 | event | C | ja-only |  |  | レン：『ルカルカ☆イントロクイズ大会～っ♪』 |
| 104 | デビューライブ | 0.80 | 1 | event | C | ja-only |  |  | デビューライブとかするのかな？ |
| 105 | カフェスペース | 0.80 | 1 | event | C | ja-only |  |  | 絵名：『カフェスペースの飾りつけに使われてる物も、 |
| 106 | 天馬司誕生祭 | 0.80 | 1 | event | C | ja-only |  |  | ようこそ、『天馬司誕生祭』へー！ |
| 107 | バーチャル・シンガーズライブ | 0.80 | 1 | event | C | ja-only |  |  | 『バーチャル・シンガーズライブ』がシブヤで開催されます |
| 108 | ライブビューイング | 0.80 | 1 | event | C | ja-only |  |  | ミク：配信やライブビューイングを |
| 109 | ニューブリーズ＆リブートフェスタ | 0.80 | 1 | event | C | ja-only |  |  | 『ニューブリーズ＆リブートフェスタ』の紹介を |
| 110 | リブートフェスタ | 0.80 | 1 | event | C | ja-only |  |  | 『ニューブリーズ＆リブートフェスタ』の紹介を |
| 111 | ライブパート | 0.80 | 1 | event | C | ja-only |  |  | MEIKO：ライブパート、楽しんでくれた？ |
| 112 | ゲーム大会する | 0.80 | 1 | event | C | ja-only |  |  | 彰人：ああ、急に『ゲーム大会する』って聞いた時は驚いたが |
| 113 | 芸術祭 | 0.80 | 1 | event | C | ja-only |  |  | レン：司くん達の学校で『芸術祭』って行事があって…… |
| 114 | スペシャルライブ | 0.80 | 1 | event | C | ja-only |  |  | 今日は、ルカの記念日スペシャルライブへようこそ！ |
| 115 | ライブコーナー | 0.80 | 1 | event | C | ja-only |  |  | 愛莉：誕生日配信でライブコーナーをやりたいって言ったのは |
| 116 | 誕生日特別ライブ！？！？ | 0.80 | 1 | event | C | ja-only |  |  | コメント：『誕生日特別ライブ！？！？』 |
| 117 | 体育祭 | 0.80 | 1 | event | C | ja-only |  |  | レン：んー、だったら『体育祭』って限定しないほうがいいかもな。 |
| 118 | チャリティーライブ | 0.80 | 1 | event | C | ja-only |  |  | お姉さん：『しかも今回は、特別チャリティーライブです！ |
| 119 | バーチャルライブ | 0.80 | 1 | event | C | ja-only |  |  | 咲希：前にやってたバーチャルライブの |
| 120 | ライブカフェバー | 0.80 | 1 | event | C | ja-only |  |  | このあたり、ライブカフェバーとかいろいろあるし |
| 121 | ライブシーン | 0.80 | 1 | event | C | ja-only |  |  | メイコさんのライブシーンも配信されたのかも |
| 122 | 褒め褒め大会 | 0.80 | 1 | event | C | ja-only |  |  | 今日はこのまま、みんなで『褒め褒め大会』がしたいでーす！ |
| 123 | ライブステージ | 0.80 | 1 | event,location | C | ja-only |  |  | 音楽院の近くに、ライブステージがあるカフェもあって！ |
| 124 | いいライブだった | 0.80 | 1 | event | C | ja-only |  |  | 『いいライブだった』 |
| 125 | シブヤ夏祭り・エクストラライブステージ | 0.80 | 1 | event,location | C | ja-only |  |  | 『シブヤ夏祭り・エクストラライブステージ』？ |
| 126 | ・エクストラライブステージ | 0.80 | 1 | event,location | C | ja-only |  |  | 『シブヤ夏祭り・エクストラライブステージ』？ |
| 127 | スリーピースライブ | 0.80 | 1 | event | C | ja-only |  |  | リン：え、めーこ姉達のスリーピースライブってこと！？ |
| 128 | ライブ頑張ってー！！ | 0.80 | 1 | event | C | ja-only |  |  | 『ライブ頑張ってー！！』 |
| 129 | ミクミク☆ワンダースポーツフェスティバル | 0.80 | 1 | event | C | ja-only |  |  | ミク：『ミクミク☆ワンダースポーツフェスティバル』 |
| 130 | ワンダースポーツフェスティバル | 0.80 | 1 | event | C | ja-only |  |  | ミク：『ミクミク☆ワンダースポーツフェスティバル』 |
| 131 | アリーナライブ | 0.80 | 1 | event | C | ja-only |  |  | 初のアリーナライブ目前ということですが。 |
| 132 | ハンバーグ祭り？ | 0.80 | 1 | event | C | ja-only |  |  | 司：『ハンバーグ祭り？』 |
| 133 | スペシャルソロライブ | 0.80 | 1 | event | C | ja-only |  |  | リン：——誕生日配信のスペシャルソロライブ、 |
| 134 | ドリームチューン・フェスタ | 0.80 | 1 | event | C | ja-only |  |  | ドリームチューン・フェスタとかでちょっとね |
| 135 | バンライブ | 0.80 | 1 | event | C | ja-only |  |  | レン：『せっかくの対バンライブだもんな』 |
| 136 | ……文化祭？ | 0.80 | 1 | event | C | ja-only |  |  | リン・レン：『……文化祭？』 |
| 137 | ザ・ライブ | 0.80 | 1 | event | C | ja-only |  |  | おおー！　ザ・ライブっていう感じのチケットだっ！ |
| 138 | ライブ必需品チェックシート | 0.80 | 1 | event | C | ja-only |  |  | やっぱり『ライブ必需品チェックシート』の |
| 139 | 鏡音リン・鏡音レン　記念日ライブ配信 | 0.80 | 1 | event,product | C | ja-only |  |  | 『鏡音リン・鏡音レン　記念日ライブ配信』……） |
| 140 | ライブスペシャルメドレー | 0.80 | 1 | event | C | ja-only |  |  | 記念日ライブスペシャルメドレー、スタート♪ |
| 141 | ワクワクゲーム大会 | 0.80 | 1 | event | C | ja-only |  |  | 冬弥：すみません、チラシにあった『ワクワクゲーム大会』に |
| 142 | 初音ミク　記念日特別ライブ配信・予告動画 | 0.80 | 1 | event,product | C | ja-only |  |  | ミク：……『初音ミク　記念日特別ライブ配信・予告動画』？ |
| 143 | ボイスライブラリー | 0.80 | 1 | event | C | ja-only |  |  | ボイスライブラリーに新しい歌声が追加されたんだっけ |
| 144 | バーチャルミニライブ | 0.80 | 1 | event | C | ja-only |  |  | バーチャルミニライブとか、楽しいブースがいっぱいで……！ |
| 145 | 出張ライブ | 0.80 | 1 | event | C | ja-only |  |  | 遥：私も『出張ライブ』っていう名前に引っ張られて、 |
| 146 | 鬼姫祭り | 0.80 | 1 | event | C | ja-only |  |  | みのり：うん！　『鬼姫祭り』っていうんだけど—— |
| 147 | 誕生日ライブ | 0.80 | 1 | event | C | ja-only |  |  | ちょっと雫、せめて『誕生日ライブ』、って言ってくれる？ |
| 148 | ワンマンライブアンコール | 0.80 | 1 | event | C | ja-only |  |  | ワンマンライブアンコール特別公演inセカイ……いくよ！ |
| 149 | ニューイヤーライブ | 0.80 | 1 | event | C | ja-only |  |  | 今日はニューイヤーライブに来てくれてありがとう！ |
| 150 | 動物ライブ | 0.80 | 1 | event | C | ja-only |  |  | もはや『動物コンセプト』じゃなくて『動物ライブ』じゃない |
| 151 | スペシャルバースデーライブ | 0.80 | 1 | event | C | ja-only |  |  | しほっちスペシャルバースデーライブ、楽しみにしててね！ |
| 152 | ハロウィンライブ | 0.80 | 1 | event | C | ja-only |  |  | ミク達のハロウィンライブ、大成功ね！ |
| 153 | バースデーライブ | 0.80 | 1 | event | C | ja-only |  |  | 今日は、わたしのバースデーライブに来てくれてありがとう！ |
| 154 | プチライブ | 0.80 | 1 | event | C | ja-only |  |  | 誕生日プチライブを楽しもうと思いまーす！ |
| 155 | バーチャル・シンガー感謝祭 | 0.80 | 1 | event | C | ja-only |  |  | もうすぐ『バーチャル・シンガー感謝祭』っていうのがあるんだ |
| 156 | ライブスタート | 0.80 | 1 | event | C | ja-only |  |  | それじゃあお待ちかねの、合同ライブスタート♪ |
| 157 | ライブパーティー | 0.80 | 1 | event | C | ja-only |  |  | MEIKO：それはね——ライブパーティーよ！ |
| 158 | メドレーライブ | 0.80 | 1 | event | C | ja-only |  |  | 愛莉：あのメドレーライブ、 |
| 159 | バレンタインフォトコンテスト | 0.80 | 1 | event | C | ja-only |  |  | 絵名：実は今、『バレンタインフォトコンテスト』 |
| 160 | 第１回　フェニックス☆ショーコンテスト | 0.80 | 1 | event | C | ja-only |  |  | 『第１回　フェニックス☆ショーコンテスト』？ |
| 161 | リアルライブ | 0.80 | 1 | event | C | ja-only |  |  | コメント：『ここからどんどんリアルライブ増やしてって欲しいな～』 |
| 162 | フェニックスワンダーランド | 0.73 | 1 | location,product | C | en,ja,ko,zh_hans,zh_tw | 菲尼克斯奇幻乐园 | Phoenix Wonderland | えむ：穂波ちゃんはフェニックスワンダーランドで |
| 163 | ポップアップストア | 0.73 | 1 | product | C | en,ja,ko,zh_hans,zh_tw | 快闪店 | Pop-up Store | ポップアップストアができたんだよ！ |
| 164 | ライリードリームパーク | 0.73 | 1 | location,product | C | en,ja,ko,zh_hans,zh_tw | 莱利梦幻乐园 | Riley Dream Park | えむ：あ、ライリードリームパークの役者さん達に来てもらうとか！ |
| 165 | ネットパラダイス | 0.73 | 1 | product | C | en,ja,ko,zh_hans,zh_tw | 网络天堂 | NetParadise | 遥：これって、ネットパラダイスの番組？ |
| 166 | お月見配信ー！ | 0.73 | 1 | product | C | ja-only |  |  | みんな：『お月見配信ー！』 |
| 167 | スケジュールアプリ | 0.73 | 1 | product | C | ja-only |  |  | 前から、いろんなスケジュールアプリを使ってみて、 |
| 168 | ベストアルバム | 0.73 | 1 | product | C | ja-only |  |  | その時買ったベストアルバムの発売記念に |
| 169 | パラダイス | 0.73 | 1 | product | C | ja-only |  |  | 俺達の愛のパラダイスへ！』 |
| 170 | 料理配信やってほしい！ | 0.73 | 1 | product | C | ja-only |  |  | コメント：『料理配信やってほしい！』 |
| 171 | 早朝配信来た！ | 0.73 | 1 | product | C | ja-only |  |  | コメント：『早朝配信来た！』 |
| 172 | ペンギンパラダイスパフェ | 0.73 | 1 | product | C | ja-only |  |  | お正月限定、新春ペンギンパラダイスパフェと、 |
| 173 | リストアップ | 0.73 | 1 | product | C | ja-only |  |  | それなら、先に直したい箇所をリストアップしていこう |
| 174 | ストア | 0.73 | 1 | product | C | ja-only |  |  | この前ストアで見たレース素材のセット！ |
| 175 | ニュースアプリ | 0.73 | 1 | product | C | ja-only |  |  | 穂波：……メッセージ？　ニュースアプリからだ |
| 176 | コラボ配信第３弾だ！ | 0.73 | 1 | product | C | ja-only |  |  | 『コラボ配信第３弾だ！』 |
| 177 | 特別緊急配信 | 0.73 | 1 | product | C | ja-only |  |  | 愛莉：今日は『特別緊急配信』っていうことで、 |
| 178 | ほぼ24時間生配信～！ | 0.73 | 1 | product | C | ja-only |  |  | 愛莉・雫：『ほぼ24時間生配信～！』 |
| 179 | アプリゲーム | 0.73 | 1 | product | C | ja-only |  |  | 一歌：志歩、それ、ペットを育てるアプリゲーム？ |
| 180 | なんちゃってブライダル配信 | 0.73 | 1 | product | C | ja-only |  |  | 瑞希：みんなの『なんちゃってブライダル配信』、最高だったな～！ |
| 181 | イラストアカウント | 0.73 | 1 | product | C | ja-only |  |  | Kが何度かえななんのイラストアカウントの話をしてたし、 |
| 182 | ドラッグストア | 0.73 | 1 | product | C | ja-only |  |  | 穂波：あ、でも先にドラッグストアに行ってきてもいいかな？ |
| 183 | 配信部屋 | 0.73 | 1 | organization,product | C | ja-only |  |  | 遥：そのほうが『配信部屋』っていうだけじゃなくて、 |
| 184 | スマホアプリ | 0.73 | 1 | product | C | ja-only |  |  | でも、スマホアプリじゃこれくらいが限界だし |
| 185 | トレーニング配信きた！ | 0.73 | 1 | product | C | ja-only |  |  | 『トレーニング配信きた！』 |
| 186 | ドラックストア | 0.73 | 1 | product | C | ja-only |  |  | ドラックストアものぞいてみたし…… |
| 187 | メッセージアプリ | 0.73 | 1 | product | C | ja-only |  |  | メッセージアプリの音声通知みたいだったな |
| 188 | 突発コラボ雑談配信！？ | 0.73 | 1 | product | C | ja-only |  |  | 『突発コラボ雑談配信！？』 |
| 189 | ワンダーステージ | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 奇幻舞台 | Wonder Stage | ワクワクわっしょいなワンダーステージへ！ |
| 190 | レコード | 0.69 | 1 | organization | C | en,ja,ko,zh_hans,zh_tw | 唱片 | Record | 杏：ねえ、レコードとか選ぶときどうしてる？ |
| 191 | メインステージ | 0.69 | 1 | location | C | ja,zh_hans | 主舞台 |  | 当たってもメインステージが遠い席だったけど…… |
| 192 | ワンダーランズ | 0.69 | 1 | organization | C | en,ja,ko,zh_hans,zh_tw | Wonderlands×Showtime | Wonderlands×Showtime | ワンダーランズ×ショウタイムだけじゃないってことだよ |
| 193 | フェニックスステージ | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 凤凰舞台 | Phoenix Stage | 類：そうだね。特にフェニックスステージとの |
| 194 | アークランド | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 弧光乐园 | Ark Land | 司：アークランドのワークショップのおかげで、 |
| 195 | プロダクション | 0.69 | 1 | organization | C | en,ja,ko,zh_hans,zh_tw | 制作公司 | Production | 音楽プロデューサーや芸能プロダクション…… |
| 196 | セカイ | 0.69 | 1 | location | C | en,ja,ko,zh_hans,zh_tw | 世界 | SEKAI | レン：こんなセカイでやってるなら、カイト達のショーも |
| 197 | タナセカイ | 0.69 | 1 | location | C | ja-only |  |  | ネネロボ：ハイ。新タナセカイをミンナで探索シマショウ |
| 198 | 実行委員企画ステージ | 0.69 | 1 | location | C | ja-only |  |  | 『実行委員企画ステージ』です |
| 199 | ブランド | 0.69 | 1 | location | C | ja-only |  |  | どこのだろう？　メジャーなブランドだったら、 |
| 200 | メリーゴーランド | 0.69 | 1 | location | C | en,ja |  | Smiley | レン：あはは。メリーゴーランドや汽車が空を飛んでるなんて、 |
| 201 | ショーステージ | 0.69 | 1 | location | C | en,ja |  | Tokyo ArcLand | 類：カイトさん、ショーステージの舞台機構を |
| 202 | コラボステージ | 0.69 | 1 | location | C | ja-only |  |  | 冬弥：コラボステージの内容が決まったのは昨日なのに、 |
| 203 | ソリス・レコード | 0.69 | 1 | organization | C | en,ja |  | You're Leo | 咲希：よーし……いざ、ソリス・レコードへ！ |
| 204 | お楽しみ委員会 | 0.69 | 1 | organization | C | ja-only |  |  | じゃあ……『お楽しみ委員会』でどう！？ |
| 205 | パーク | 0.69 | 1 | location | C | ja-only |  |  | 穂波：『バウバウわんだふるパーク』っていうところに行くんだ |
| 206 | ルーキーステージ | 0.69 | 1 | location | C | ja-only |  |  | ルーキーステージのライブ、結構よかったな |
| 207 | ファーストステージ | 0.69 | 1 | location | C | ja-only |  |  | 彰人：まあつっても、まだファーストステージだ。 |
| 208 | ライリー・エンターテインメント | 0.69 | 1 | organization | C | en,ja |  | Entertainment | 慶介：俺達は明日、ライリー・エンターテインメント社と、 |
| 209 | ステージパフォーマンス | 0.69 | 1 | location | C | ja-only |  |  | MEIKO：ええ！　愛莉ちゃんのステージパフォーマンスは |
| 210 | バックステージ | 0.69 | 1 | location | C | ja-only |  |  | 小学生の志歩：え？　こっちって……バックステージでしょ。 |
| 211 | アイランド | 0.69 | 1 | location | C | ja-only |  |  | キッチンはオシャレなアイランド型のようです |
| 212 | ランド | 0.69 | 1 | location | C | ja-only |  |  | 司：ランド内にあるレストランでフルーツパフェを食べさせて |
| 213 | 部活動ステージ | 0.69 | 1 | location,organization | C | ja-only |  |  | ステージ係長：ひとつは、『部活動ステージ』です。 |
| 214 | サブステージ | 0.69 | 1 | location | C | ja-only |  |  | 実力派バンドが集まるサブステージ—— |
| 215 | スパークル | 0.69 | 1 | location | C | ja-only |  |  | 咲希：あれって……えっと、スパークルのマスキングテープ？ |
| 216 | アークランドチーム | 0.69 | 1 | location | C | ja-only |  |  | 俺達アークランドチームが出るなんて思わないよな |
| 217 | アークランドキャスト | 0.69 | 1 | location | C | ja-only |  |  | アークランドキャストA：お！　ワンダーランズ×ショウタイム、来てくれたのか！ |
| 218 | レインボーステージ | 0.69 | 1 | location | C | ja-only |  |  | 櫻子：特に、レインボーステージというステージが好きで、 |
| 219 | ステージエリア | 0.69 | 1 | location | C | ja-only |  |  | ステージエリアのMCを担当することになって—— |
| 220 | グラウンド | 0.69 | 1 | location | C | ja-only |  |  | 吹奏楽部もグラウンド走ってたよね |
| 221 | スポジョイパーク | 0.69 | 1 | location | C | ja-only |  |  | 瑞希：どうせなら、みんなで１日中スポジョイパーク満喫！とか |
| 222 | スターアイランド | 0.69 | 1 | location | C | ja-only |  |  | 寧々：スターアイランド……だっけ |
| 223 | アニマルランド | 0.69 | 1 | location | C | en,ja |  | Animal Land | 遥：昨日、みのりがアニマルランドに |
| 224 | 森ノ宮音楽学園 | 0.69 | 1 | location | C | ja-only |  |  | 有田：皆さんには今回、併設している『森ノ宮音楽学園』で |
| 225 | コスメブランド | 0.69 | 1 | location | C | ja-only |  |  | プチバズしてたコスメブランドでしょ。また新作出るの？ |
| 226 | ステージイベント | 0.69 | 1 | location | C | ja-only |  |  | えむ：イベント制作会社なんだ！　ステージイベントとか、 |
| 227 | ステージング | 0.69 | 1 | location | C | en,ja |  | Staging Stuff | 穂波：ライブのステージングについて話し合って、 |
| 228 | ステージキャスト | 0.69 | 1 | location | C | ja-only |  |  | ステージキャスト：——ご来場の皆さま、ワンダーステージへようこそ！ |
| 229 | 広くてすてきな部屋ね～！ | 0.69 | 1 | organization | C | ja-only |  |  | 愛莉：普段だったら、『広くてすてきな部屋ね～！』とか |
| 230 | お台場アニマルランド | 0.69 | 1 | location | C | ja-only |  |  | 『お台場アニマルランド』って、たしか屋内動物園だったよね |
| 231 | チョコブランド | 0.69 | 1 | location | C | ja-only |  |  | チョコブランド『クロレ・ショコラ』と |
| 232 | エンターテインメント時代劇 | 0.69 | 1 | organization | C | ja-only |  |  | 類：三日月組と言えば、『エンターテインメント時代劇』 |
| 233 | エンターテインメント | 0.69 | 1 | organization | C | en,ja |  | Theatrical Performan | 類：三日月組と言えば、『エンターテインメント時代劇』 |
| 234 | グランドピアノ | 0.69 | 1 | location | C | ja-only |  |  | ミオ：グランドピアノがあって、誰かが演奏してたり、 |
| 235 | セカイ—— | 0.69 | 1 | location | C | ja-only |  |  | 奏：『セカイ——』 |
| 236 | エンターテイメント | 0.69 | 1 | organization | C | ja-only |  |  | 冬弥：音楽だけじゃなく、エンターテイメントや政治、 |
| 237 | ステージスタッフ | 0.69 | 1 | location | C | en,ja |  | Th-This Christmas | ステージスタッフ：そうだね……制作側にも、 |
| 238 | Meowパーク | 0.69 | 1 | location | C | ja-only |  |  | 犬山：みんな、猫の遊園地——『Meowパーク』へようこそ！ |
| 239 | ステージマネージャー | 0.69 | 1 | location | C | ja-only |  |  | 櫻子：あとは、うちのステージマネージャーと |
| 240 | ワンダーステージキャスト | 0.69 | 1 | location | C | ja-only |  |  | ワンダーステージキャストA：鳳さん、お疲れさまです。 |
| 241 | 予選ステージ | 0.69 | 1 | location | C | ja-only |  |  | 『予選ステージ』と『決勝ステージ』の２部構成—— |
| 242 | 決勝ステージ | 0.69 | 1 | location | C | ja-only |  |  | 『予選ステージ』と『決勝ステージ』の２部構成—— |
| 243 | アマチュアステージ | 0.69 | 1 | location | C | ja-only |  |  | 誰でも参加可能なアマチュアステージと、 |
| 244 | スペシャルステージ | 0.69 | 1 | location | C | ja-only |  |  | 野外スペシャルステージに—— |
| 245 | 東京アークランド | 0.69 | 1 | location | C | ja-only |  |  | 司：あの、『東京アークランド』か！？ |
| 246 | ラ部アイドル | 0.69 | 1 | organization | C | ja-only |  |  | 『ラ部アイドル』のインタビューも受けれるようになるんだ |
| 247 | フェニックスワンダーランドマスター | 0.69 | 1 | location | C | ja-only |  |  | みのり：わっ！　さすが、フェニックスワンダーランドマスター！ |
| 248 | アパレルブランド | 0.69 | 1 | location | C | ja-only |  |  | 絵名：あ、それ日本初出店っていうアパレルブランドの？ |
| 249 | ワンダーランド | 0.69 | 1 | location | C | ja-only |  |  | このワンダーランドは、ミクミク軍団が占拠したー！！！ |
| 250 | エンターテインメントショー | 0.69 | 1 | organization | C | ja-only |  |  | 冬弥：だが、様々なエンターテインメントショーを |
| 251 | HopeHope♡Heart　ステージオン！ | 0.69 | 1 | location | C | ja-only |  |  | 全員：『HopeHope♡Heart　ステージオン！』 |
| 252 | ステージオン | 0.69 | 1 | location | C | ja-only |  |  | 全員：『HopeHope♡Heart　ステージオン！』 |
| 253 | セカイピース | 0.69 | 1 | location | C | ja-only |  |  | 肝心の『セカイピース』の話は全然聞けなかったね |
| 254 | 決勝ステージ進出グループ決定！ | 0.69 | 1 | location | C | ja-only |  |  | 遥：『決勝ステージ進出グループ決定！』か…… |
| 255 | ピューロランド | 0.69 | 1 | location | C | ja-only |  |  | 実はぼく、最初はここをピューロランドだと思ってたから！ |
| 256 | マリーランド | 0.69 | 1 | location | C | ja-only |  |  | マリーランドにかえったら、おばあちゃんにみせてあげるの |
| 257 | ミュージックランド | 0.69 | 1 | location | C | ja-only |  |  | こはね：昨日のミュージックランド、見たよ！ |
| 258 | ニューレコード | 0.69 | 1 | organization | C | ja-only |  |  | 寧々：そう。中には、断トツでニューレコード出してるのもあるし。 |
| 259 | アークランドリベンジ | 0.69 | 1 | location | C | ja-only |  |  | 司：今、アークランドリベンジ計画について話していたのだ |
| 260 | 運動部応援・超スタミナ丼 | 0.69 | 1 | organization | C | ja-only |  |  | その名も、『運動部応援・超スタミナ丼』！ |
| 261 | 事務所 | 0.69 | 1 | organization | C | ja-only |  |  | 絶対に“事務所”ってところに入らないといけないの？ |
| 262 | ナンバーワンステージ | 0.69 | 1 | location | C | ja-only |  |  | このフェニックスワンダーランドのナンバーワンステージだ、 |
| 263 | ネバーランド | 0.69 | 1 | location | C | ja-only |  |  | ピーターパン：『その最果ての島、ネバーランドへ！』 |
| 264 | フェニックスワンダーランド・オープン | 0.69 | 1 | location | C | ja-only |  |  | 司：フェニックスワンダーランド・オープン記念公演は |
| 265 | ボロステージ | 0.69 | 1 | location | C | ja-only |  |  | ただのボロステージとしか思わなかったが—— |
| 266 | ランランランランドセル | 0.69 | 1 | location | C | ja-only |  |  | えむ：ランランランランドセル～っ、 |
| 267 | ラ部アイドル・インタビュー | 0.69 | 1 | organization | C | ja-only |  |  | 『ラ部アイドル・インタビュー』の開始時間、かなり近いですよね |
| 268 | サンリオピューロランド | 0.69 | 1 | location | C | ja-only |  |  | 志歩：まあ、正確には出張版のサンリオピューロランドだけど |
| 269 | サテライトピューロランド | 0.69 | 1 | location | C | ja-only |  |  | 似てるよね、今日行ったサテライトピューロランドに |
| 270 | モアモアパーク | 0.69 | 1 | location | C | ja-only |  |  | 愛莉：ふふ、記念すべきモアモアパークの目玉だものね！ |
| 271 | 吸血鬼部屋 | 0.69 | 1 | organization | C | ja-only |  |  | みのり：えへへ、他にも『吸血鬼部屋』って呼ばれてた |
| 272 | 新しいセカイ探検隊 | 0.69 | 1 | location | C | ja-only |  |  | ミク：うん☆　ミク達、『新しいセカイ探検隊』なんだ〜♪ |
| 273 | ガーランド | 0.69 | 1 | location | C | ja-only |  |  | お花のモビールとガーランドよ！ |
| 274 | バウバウわんだふるパーク | 0.69 | 1 | location | C | ja-only |  |  | 穂波：『バウバウわんだふるパーク』っていうところに行くんだ |
| 275 | バイ菌なんて全部やっつけてやる！ | 0.69 | 1 | organization | C | ja-only |  |  | 『バイ菌なんて全部やっつけてやる！』って言ってくれて—— |
| 276 | セカイクイズ | 0.69 | 1 | location | C | ja-only |  |  | セカイクイズ大会～！ |
| 277 | 刺しゅう教室ふたたび！ | 0.69 | 1 | location | C | ja-only |  |  | コメント：『刺しゅう教室ふたたび！』 |
| 278 | ステージギリギリ | 0.69 | 1 | location | C | ja-only |  |  | 類：王子が魔女にステージギリギリまで追い詰められ、 |
| 279 | ランドセル | 0.69 | 1 | location | C | ja-only |  |  | じゃあオレ、ランドセル置いてくるね |
| 280 | ——フェニックスワンダーランド！ | 0.69 | 1 | location | C | ja-only |  |  | えむ・リン：『——フェニックスワンダーランド！』 |
| 281 | 虹色ガーランド | 0.69 | 1 | location | C | ja-only |  |  | 『虹色ガーランド』……！） |
| 282 | ブランドイメージ | 0.69 | 1 | location | C | ja-only |  |  | 会社のブランドイメージを高められる』んだって！ |
| 283 | セカンドステージ | 0.69 | 1 | location | C | ja-only |  |  | 次のセカンドステージも勝たなきゃ先には進めねえ |
| 284 | ワンダーランズ×ショウタイム公演！ | 0.69 | 1 | organization | C | ja-only |  |  | 『ワンダーランズ×ショウタイム公演！』って |
| 285 | スーパーロングスパーク | 0.69 | 1 | location | C | ja-only |  |  | スーパーロングスパークにしよーっと！ |
| 286 | 決勝ステージ頑張れ | 0.69 | 1 | location | C | ja-only |  |  | みのり：うん！　『決勝ステージ頑張れ』とか |
| 287 | ステージセット | 0.69 | 1 | location | C | ja-only |  |  | もっとステージセットにこだわってみる……？ |
| 288 | ヒーローステージ | 0.69 | 1 | location | C | ja-only |  |  | 司：まあいくつかは……ヒーローステージでは、 |
| 289 | スーパークリスマス | 0.69 | 1 | location | C | ja-only |  |  | 記念日のお祝いも合体してたスーパークリスマス会だから！ |
| 290 | ナンバーワンショーステージ | 0.69 | 1 | location | C | ja-only |  |  | ナンバーワンショーステージの歌姫——青龍院櫻子よ！ |
| 291 | 全部真っ白なソフトクリーム屋さん | 0.69 | 1 | organization | C | ja-only |  |  | 一歌：……えっと、『全部真っ白なソフトクリーム屋さん』？ |
| 292 | No.9レコード | 0.69 | 1 | organization | C | ja-only |  |  | “No.9レコード”ってとこなんだが |
| 293 | SHINOBIステージ | 0.69 | 1 | location | C | ja-only |  |  | 奏：『SHINOBIステージ』……って、書いてるね |
| 294 | バーチャル・シンガー・グランドショーケース | 0.69 | 1 | location | C | ja-only |  |  | 『バーチャル・シンガー・グランドショーケース』？ |
| 295 | グランドショーケース | 0.69 | 1 | location | C | ja-only |  |  | 一歌：グラショ——えっと、グランドショーケースは |
| 296 | コラボステージハ | 0.69 | 1 | location | C | ja-only |  |  | 夏目：うン。特に今日開催されるコラボステージハ、 |
| 297 | セカイダヨー | 0.69 | 1 | location | C | ja-only |  |  | ミクの着ぐるみ：ようこそ！　ここは、キミの想いのセカイダヨー！ |
| 298 | SEKAI | 0.69 | 1 | location | C | ja-only |  |  | 『STAGE OF SEKAI』！ |
| 299 | ステージチェンジ | 0.69 | 1 | location | C | ja-only |  |  | まずは——ステージチェンジ！ |
| 300 | セカイカフェ | 0.69 | 1 | location | C | ja-only |  |  | ずーっと楽しみにしてた『セカイカフェ』—— |

## 待复核：高频 other（可能漏贴 tag）

| canonical | weight | occ | tags | evidence |
|-----------|--------|-----|------|----------|
