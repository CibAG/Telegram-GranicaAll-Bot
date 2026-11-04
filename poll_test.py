import requests

urls = [
    "https://granica.gov.pl/index.php/ru/?option=com_content&view=article&id=207&p=b&c=t&v=ru&k=w",
    "https://granica.gov.pl/index.php/pl/?option=com_content&view=article&id=207&p=b&c=t&v=pl&k=w",
    "https://granica.gov.pl/index_wait.php?v=ru",
]
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

for url in urls:
    try:
        r = requests.get(url, timeout=20, headers=headers)
        t = r.text.lower()
        print(url[:70])
        print("   статус:", r.status_code, "| длина:", len(r.text))
        print("   Тересполь есть:", ("terespol" in t or "тересполь" in t))
        print("   таблиц <table>:", t.count("<table"))
        print("   строки времен:", ("szacunkowy czas" in t or "примерное время" in t))
        print("-" * 40)
    except Exception as e:
        print(url[:70], "ОШИБКА:", e)