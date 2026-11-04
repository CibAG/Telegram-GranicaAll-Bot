# ... (Весь код остаётся точно таким же, как в предыдущем полном варианте) ...
# Единственное изменение в функции build_report, чтобы красиво обработать пустую очередь:

def build_report(d, zone_key: str = "brest", car_filter=None, interval: int = None, avg_period: int = 24, history_count: int = 0, urgent_mode: bool = False):
    zone_name = get_zone_name(zone_key)
    car_line = get_car_status_line(d.get("sorted_cars", []), car_filter, d.get("stats_hour", 0))
    filter_block = f"{car_line}━━━━━━━━━━━━━━━\n" if car_line else ""
    
    if urgent_mode:
        interval_text = " 🚨 Машина вызвана в ПП — опрос каждые 30 сек"
    else:
        interval_text = f" Периодом {interval} мин." if interval else ""
    
    if history_count > 0:
        avg_line = f"📊 На основе {history_count} записей за {avg_period} ч\n"
    else:
        avg_line = "📊 По текущим данным\n"
    
    # Если статистика есть, а очередь пуста – значит, оператор скрыл данные
    queue_note = ""
    if d["total"] == 0 and d["stats_hour"] > 0:
        queue_note = "⚠️ <i>Официальный API временно не отдаёт живую очередь. Показана только статистика.</i>\n"

    return (
        f"📊 <b>Мониторинг границы {zone_name}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{filter_block}"
        f"🚗 Машин в очереди: {html.escape(str(d['total']))}\n"
        f"🕒 Первая стоит: {html.escape(str(d['wait']))} мин.\n"
        f"⏳ Оценка для вас: {format_estimate_html(d['estimate'])}\n"
        f"{queue_note}"
        f"{avg_line}"
        f"📉 За час: {html.escape(str(d['stats_hour']))} маш.\n"
        f"📅 За сутки: {html.escape(str(d['stats_day']))} маш.\n"
        f"📅 Дата 1-го авто: {html.escape(str(d['reg_date']))}\n"
        f"🔄 Статус изменён: {html.escape(str(d['changed']))}\n"
        "━━━━━━━━━━━━━━━\n"
        f"💡 <i>Мониторинг работает в фоновом режиме.{interval_text}</i>"
    )