"""
楽天市場の商品データを取得・加工して、Google スプレッドシートに保存するプログラム

使い方：
    python main.py                      … items.txt の商品を取得して、シートに保存する
    python main.py --search キーワード  … 楽天で商品を探し、items.txt に書く商品コードを表示する

流れ：
    1. items.txt に書いた楽天の商品コード（お店:番号）を読む
    2. 楽天市場 商品検索 API で、商品ごとに価格・在庫などを取得する
    3. 前回の価格と比べて、差額と値動き（値上がり／値下がり など）を付ける
    4. スプレッドシートの「最新」シートを書き直し、「履歴」シートの下に追加する

事前準備は README.md を見てください。
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from google.auth.exceptions import GoogleAuthError, TransportError
from google.oauth2.service_account import Credentials

# ===== 設定（ここを変えれば動きを変えられる） =====
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"                          # 楽天の鍵とシート ID を書いたファイル
SERVICE_ACCOUNT_FILE = BASE_DIR / "service_account.json"  # Google のサービスアカウントの鍵
ITEMS_FILE = BASE_DIR / "items.txt"                   # 管理する商品の商品コードを1行ずつ書いたファイル

# 楽天市場 商品検索 API（2026年からの新しい接続先）
RAKUTEN_API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
# 楽天のアプリ登録の「許可されたWebサイト」に書いたサイト。頼むときに「ここから来ました」と付ける
APP_SITE_URL = "https://github.com/m-tsuna/rakuten-items-to-sheet"
WAIT_SECONDS = 1      # 楽天に頼む間隔（短い間に何度も頼むと止められるため）
TIMEOUT_SECONDS = 10  # 楽天の返事を待つ最大の秒数
SEARCH_HITS = 10      # --search で表示する商品の数

# スプレッドシートの読み書きだけを許可する（Drive 全体の権限は付けない）
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LATEST_SHEET = "最新"   # 商品1つ1行。毎回書き直す
HISTORY_SHEET = "履歴"  # 実行するたびに下へ追加する
COLUMNS = [
    "取得日時", "商品コード", "商品名", "ショップ", "価格（円）", "前回の価格（円）",
    "差額（円）", "値動き", "在庫", "レビュー件数", "レビュー平均", "商品ページ",
]
PRICE_COLUMN = COLUMNS.index("価格（円）")  # 前回の価格を読むときに使う列の位置

JST = timezone(timedelta(hours=9))  # 日本時間
# ===================================================


def stop(message):
    """エラーの内容を日本語で表示して、プログラムを終わらせる"""
    print(f"エラー：{message}")
    sys.exit(1)


def load_settings():
    """.env から楽天の鍵とスプレッドシート ID を読み、空の項目があれば知らせて終わる"""
    if not ENV_FILE.exists():
        stop(f".env が見つかりません（{ENV_FILE}）。.env.example をコピーして .env を作り、鍵を書いてください。")
    # すでに環境変数にある値は上書きしない（テストで1項目だけ差し替えられるように）
    load_dotenv(ENV_FILE)

    names = ["RAKUTEN_APPLICATION_ID", "RAKUTEN_ACCESS_KEY", "SPREADSHEET_ID"]
    settings = {name: (os.getenv(name) or "").strip() for name in names}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        stop(".env の次の項目が空です：" + "、".join(missing))

    settings["SPREADSHEET_ID"] = extract_spreadsheet_id(settings["SPREADSHEET_ID"])
    return settings


def extract_spreadsheet_id(text):
    """シートの URL 全体が貼られていても、/d/ と / の間の ID だけを取り出す"""
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", text)
    return match.group(1) if match else text


def parse_item_code(line):
    """items.txt の1行から商品コード（お店:番号）を取り出す。形が違えば None を返す

    例：「livingut:10020902  # 水筒」→ livingut:10020902（# から後ろはメモ）
    """
    code = line.split("#")[0].strip()
    return code if re.fullmatch(r"[A-Za-z0-9_-]+:[0-9]+", code) else None


def load_item_codes():
    """items.txt を読み、商品コードの一覧を返す（読めない行は何行目か表示して飛ばす）"""
    if not ITEMS_FILE.exists():
        stop(f"items.txt が見つかりません（{ITEMS_FILE}）。商品コードを1行ずつ書いて作ってください（探し方：python main.py --search キーワード）。")

    codes = []
    lines = ITEMS_FILE.read_text(encoding="utf-8").splitlines()
    for line_no, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):  # 空行とメモの行は飛ばす
            continue
        code = parse_item_code(line)
        if code is None:
            print(f"注意：items.txt の {line_no} 行目は商品コード（お店:番号）の形ではないので飛ばします：{line}")
        elif code not in codes:  # 同じ商品が2回書かれていても1回だけ取る
            codes.append(code)

    if not codes:
        stop("items.txt に商品コードがありません。python main.py --search キーワード で探して、1行ずつ書いてください。")
    return codes


def request_rakuten(session, settings, conditions):
    """楽天の商品検索 API に頼み、見つかった商品のリストを返す

    conditions：検索の条件（{"itemCode": "..."} や {"keyword": "..."}）
    返事の形（formatVersion=2 のとき）：
        {"count": 1, "Items": [{"itemName": "...", "itemPrice": 3980, "availability": 1, ...}]}
    """
    params = {
        "applicationId": settings["RAKUTEN_APPLICATION_ID"],
        "availability": 0,   # 0 = 売り切れの商品も返してもらう
        "formatVersion": 2,  # 返事の形を簡単な方にする
        **conditions,
    }
    headers = {
        # アクセスキーは URL ではなく見出しで送る（エラーの表示に URL が出ても鍵が見えないように）
        "accessKey": settings["RAKUTEN_ACCESS_KEY"],
        # 楽天に登録した「許可されたWebサイト」から来た、という印
        "Origin": "https://github.com",
        "Referer": APP_SITE_URL,
    }
    try:
        response = session.get(RAKUTEN_API_URL, params=params, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        stop("楽天から返事がありません。しばらくしてからもう一度実行してください。")
    except requests.exceptions.RequestException:
        stop("楽天につながりません。インターネットの接続を確かめてください。")

    # 楽天は失敗すると、理由を次のどちらかの形で返す
    #   {"error": "wrong_parameter", "error_description": "itemCode is not valid"}
    #   {"errors": {"errorCode": 403, "errorMessage": "Invalid Access Key"}}
    try:
        data = response.json()
    except ValueError:
        data = {}
    errors = data.get("errors") if isinstance(data.get("errors"), dict) else {}
    reason = " ".join(str(part) for part in [
        data.get("error", ""), data.get("error_description", ""), errors.get("errorMessage", ""),
    ] if part)

    if response.status_code == 200:
        return data.get("Items", [])
    # 商品コードの形が正しくない・お店が無いときは 400 で「itemCode」と返ってくる → 見つからない扱い
    if response.status_code == 404 or (response.status_code == 400 and "itemCode" in reason):
        return []
    if response.status_code == 429:
        stop("楽天に短い間に頼みすぎました。しばらく待ってから実行してください。")
    if response.status_code in (400, 401, 403):
        stop(
            f"楽天に断られました（{response.status_code}：{reason}）。"
            ".env のアプリ ID・アクセスキーと、楽天のアプリ登録の「許可されたWebサイト」を確かめてください。"
        )
    stop(f"楽天でエラーが起きました（{response.status_code}：{reason}）。しばらくしてからもう一度実行してください。")


def fetch_item(session, settings, item_code):
    """商品コードで商品を1つ取得する。見つからなければ（販売終了など）None を返す"""
    items = request_rakuten(session, settings, {"itemCode": item_code})
    return items[0] if items else None


def search_items(keyword):
    """キーワードで楽天を探し、items.txt に書く商品コードと一緒に表示する（シートには書かない）"""
    settings = load_settings()
    with requests.Session() as session:
        items = request_rakuten(session, settings, {
            "keyword": keyword,
            "hits": SEARCH_HITS,
            "sort": "-reviewCount",  # レビューの多い順（よく売れている商品から）
        })
    if not items:
        print(f"「{keyword}」の商品は見つかりませんでした。")
        return
    print(f"「{keyword}」の商品（レビューの多い順）。使う商品の商品コードを items.txt に書いてください。")
    print("（URL を Cmd ＋ クリックすると、ブラウザで画像や詳細を見られます）")
    for number, item in enumerate(items, start=1):
        stock = "在庫あり" if item.get("availability") == 1 else "売り切れ"
        # URL の ? から後ろは楽天が付けた印なので、短くするために外す
        page_url = item.get("itemUrl", "").split("?")[0]
        print(f"{number:>2}. {item['itemCode']:<28} {item['itemPrice']:>8,}円 {stock}  {item['itemName'][:30]}")
        print(f"    {page_url}")


def describe_change(price, previous_price):
    """今回と前回の価格から、差額と値動きの言葉を作る"""
    if price is None:
        return "", "見つからない"
    if previous_price is None:
        return "", "初回"
    diff = price - previous_price
    if diff > 0:
        return diff, "値上がり"
    if diff < 0:
        return diff, "値下がり"
    return 0, "変わらず"


def build_row(item_code, item, previous_price, fetched_at):
    """取得した商品データを、シートの1行（COLUMNS の順）に加工する"""
    if item is None:  # 販売終了や URL の間違いで見つからなかった商品
        diff, change = describe_change(None, previous_price)
        return [fetched_at, item_code, "（見つかりませんでした）", "", "",
                previous_price if previous_price is not None else "", diff, change, "", "", "", ""]

    price = item.get("itemPrice")
    diff, change = describe_change(price, previous_price)
    stock = "在庫あり" if item.get("availability") == 1 else "売り切れ"
    return [
        fetched_at,
        item_code,
        item.get("itemName", ""),
        item.get("shopName", ""),
        price,
        previous_price if previous_price is not None else "",
        diff,
        change,
        stock,
        item.get("reviewCount", ""),
        item.get("reviewAverage", ""),
        item.get("itemUrl", ""),
    ]


def google_error_message(error):
    """Google から返ってきたエラーを、直し方つきの日本語にする"""
    status = getattr(error.response, "status_code", None)
    detail = getattr(error, "error", {}).get("message", "") if isinstance(getattr(error, "error", None), dict) else ""
    if status == 403 and ("has not been used" in detail or "disabled" in detail):
        return f"Google Sheets API が有効になっていません。Google Cloud Console で有効にしてください（{detail}）"
    if status in (403, 404):
        return (f"スプレッドシートを開けません（{status}）。.env の SPREADSHEET_ID と、"
                "シートをサービスアカウントのメールアドレスに「編集者」で共有したかを確かめてください。")
    return f"Google でエラーが起きました（{status}：{detail}）"


def open_spreadsheet(spreadsheet_id):
    """サービスアカウントの鍵で Google にログインし、スプレッドシートを開く"""
    if not SERVICE_ACCOUNT_FILE.exists():
        stop(f"service_account.json が見つかりません（{SERVICE_ACCOUNT_FILE}）。README.md の手順で用意してください。")
    try:
        credentials = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_FILE), scopes=SCOPES)
        client = gspread.authorize(credentials)
        return client.open_by_key(spreadsheet_id)
    except (ValueError, KeyError):
        stop("service_account.json の中身が正しくありません。Google Cloud Console で作り直してください。")
    except gspread.exceptions.SpreadsheetNotFound:
        stop("スプレッドシートが見つかりません。.env の SPREADSHEET_ID と、サービスアカウントへの共有を確かめてください。")
    except gspread.exceptions.APIError as error:
        stop(google_error_message(error))
    except TransportError:  # ログインの途中で通信できなかった
        stop("Google につながりません。インターネットの接続を確かめてください。")
    except GoogleAuthError:
        stop("Google にログインできません。service_account.json の鍵が無効になっていないか確かめてください。")
    except requests.exceptions.RequestException:
        stop("Google につながりません。インターネットの接続を確かめてください。")


def get_worksheet(spreadsheet, title):
    """名前でシートを探し、無ければ作って1行目に見出しを入れる"""
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=100, cols=len(COLUMNS))
        worksheet.update([COLUMNS], "A1")
        return worksheet


def read_previous_prices(latest_sheet):
    """「最新」シートから、商品コードごとの前回の価格を読む（前回が無ければ空の辞書）"""
    prices = {}
    for row in latest_sheet.get_all_values()[1:]:  # 1行目は見出しなので飛ばす
        if len(row) <= PRICE_COLUMN:
            continue
        code, price = row[1], row[PRICE_COLUMN].replace(",", "")
        prices[code] = int(price) if price.isdigit() else None
    return prices


def save_rows(latest_sheet, history_sheet, rows):
    """「最新」シートを書き直し、「履歴」シートの下に今回の行を足す"""
    latest_sheet.clear()
    latest_sheet.update([COLUMNS] + rows, "A1")
    if not history_sheet.get_all_values():  # 履歴が空なら見出しから入れる
        history_sheet.update([COLUMNS], "A1")
    # RAW = 書いた文字をそのまま入れる（商品名が = で始まっても式にしない）
    history_sheet.append_rows(rows, value_input_option="RAW")


def format_result(row):
    """画面に出す1行（価格・前回比・在庫）を作る"""
    name, price, diff, change, stock = row[2], row[4], row[6], row[7], row[8]
    if change == "見つからない":
        return f"{row[1]} … 見つかりませんでした（販売終了か、商品コードの間違い）"
    if diff == "":       # 初回
        detail = change
    elif diff == 0:
        detail = "前回と同じ"
    else:
        detail = f"前回比 {diff:+,}円 {change}"
    return f"{name[:30]} … {price:,}円（{detail}）{stock}"


def main():
    # python main.py --search キーワード のときは、商品を探して表示するだけ
    if len(sys.argv) >= 2 and sys.argv[1] == "--search":
        keyword = " ".join(sys.argv[2:]).strip()
        if not keyword:
            stop("探すキーワードを書いてください（例：python main.py --search 水筒）。")
        search_items(keyword)
        return

    settings = load_settings()
    item_codes = load_item_codes()

    # 先にシートを開いておく（シートの設定の間違いに、楽天に頼む前に気づける）
    spreadsheet = open_spreadsheet(settings["SPREADSHEET_ID"])
    try:
        latest_sheet = get_worksheet(spreadsheet, LATEST_SHEET)
        history_sheet = get_worksheet(spreadsheet, HISTORY_SHEET)
        previous_prices = read_previous_prices(latest_sheet)
    except gspread.exceptions.APIError as error:
        stop(google_error_message(error))

    print(f"商品 {len(item_codes)} 件のデータを楽天から取得します…")
    fetched_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    rows = []
    with requests.Session() as session:
        for number, item_code in enumerate(item_codes, start=1):
            if number > 1:
                time.sleep(WAIT_SECONDS)
            item = fetch_item(session, settings, item_code)
            row = build_row(item_code, item, previous_prices.get(item_code), fetched_at)
            rows.append(row)
            print(f"  {number}/{len(item_codes)} {format_result(row)}")

    try:
        save_rows(latest_sheet, history_sheet, rows)
    except gspread.exceptions.APIError as error:
        stop(google_error_message(error))
    except requests.exceptions.RequestException:
        stop("Google につながりません。インターネットの接続を確かめてください。")

    print(f"スプレッドシートに保存しました（最新：{len(rows)}行／履歴：{len(rows)}行を追加）")
    print(spreadsheet.url)


if __name__ == "__main__":
    main()
