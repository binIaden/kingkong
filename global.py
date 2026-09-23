import asyncio
import os
import time
import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ============================================================
# CONFIGURACIÓN
# ============================================================
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

# Bot único (recibe comandos Y envía triggers)
BOT = "@KingKongccs2bot"
TRIGGER_USERNAME = "kingkongccs2bot"   # mismo bot

# Palabra clave para trigger manual (Saved Messages)
MANUAL_TRIGGER_WORD = "run_test"

# Cooldown entre flujos (segundos) para evitar cadenas
TRIGGER_COOLDOWN = 5

SESSION_STRING = os.environ.get("TELEGRAM_SESSION", "").strip()

PRODUCTOS_FILE = "productos.txt"
MAX_PRICE = 6.0

TIMEOUT = 45
CLICK_TIMEOUT = 3
MAX_RETRIES = 3
RETRY_SLEEP = 1
HEADER_ATTEMPTS = 2
CHECK_ATTEMPTS = 2

POLL_INTERVAL = 0.5
MAX_PAGES = 300

if not os.path.exists(PRODUCTOS_FILE):
    with open(PRODUCTOS_FILE, "w", encoding="utf-8") as f:
        f.write(os.environ.get("PRODUCTOS_CONTENT", ""))

client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else "telegram_session",
    API_ID,
    API_HASH
)

INSUFFICIENT_MSG = "Current user's account balance is insufficient. Please return to the homepage to recharge or adjust the amount."
CARD_HEADER_FAIL_MSG = "If you fail to obtain the card header information, please check the card head again"

used_buttons = set()

BOT_ID = None

refund_detected = False
refund_event = asyncio.Event()

# Anti-cadena: timestamp del último flujo iniciado
last_flow_start = 0

# ============================================================
# MÉTRICAS
# ============================================================

METRICS = {}

def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg):
    print(f"[{_ts()}] {msg}")

def reset_metrics(trigger_name):
    global METRICS
    METRICS = {
        "run_start": time.monotonic(),
        "run_end": None,
        "trigger_name": trigger_name,
        "pages_visited": 0,
        "items_analyzed": 0,
        "items_valid": 0,
        "purchases_ok": 0,
        "purchases_order_failed": 0,
        "purchases_insufficient": 0,
        "purchases_header_fail": 0,
        "purchases_no_response": 0,
        "purchases_check_missing": 0,
        "clicks_total": 0,
        "clicks_timeout": 0,
        "clicks_error": 0,
        "clicks_success_first_try": 0,
        "clicks_success_after_retry": 0,
        "header_card_retries": 0,
        "header_check_retries": 0,
        "refunds_detected": 0,
        "response_times": [],
        "page_times": [],
        "purchase_times": [],
        "phase_times": {},
    }

def fmt_secs(s):
    if s < 60:
        return f"{s:.2f}s"
    mins = int(s // 60)
    secs = s - mins * 60
    return f"{mins}m{secs:.1f}s"

def print_run_summary():
    m = METRICS
    if not m or m.get("run_start") is None:
        return
    end = m["run_end"] if m["run_end"] is not None else time.monotonic()
    total = end - m["run_start"]

    print("\n" + "╔" + "═" * 70 + "╗")
    print("║" + " " * 22 + "RESUMEN DE EJECUCIÓN" + " " * 28 + "║")
    print("╚" + "═" * 70 + "╝")
    print(f"  Trigger:               @{m['trigger_name']}")
    print(f"  Duración total:        {fmt_secs(total)}")
    print(f"  Páginas visitadas:     {m['pages_visited']}")
    print(f"  Artículos analizados:  {m['items_analyzed']}")
    print(f"  Artículos válidos:     {m['items_valid']}")
    print()
    print(f"  ✅ Compras OK:         {m['purchases_ok']}")
    print(f"  ✗ Order failed:        {m['purchases_order_failed']}")
    print(f"  ✗ Error cabecera:      {m['purchases_header_fail']}")
    print(f"  ✗ Sin respuesta:       {m['purchases_no_response']}")
    print(f"  ✗ Check faltante:      {m['purchases_check_missing']}")
    print(f"  ✗ Saldo insuficiente:  {m['purchases_insufficient']}")
    print(f"  💰 Refunds:            {m['refunds_detected']}")
    print()
    print(f"  Clics totales:         {m['clicks_total']}")
    print(f"    - Éxito 1er intento: {m['clicks_success_first_try']}")
    print(f"    - Éxito tras retry:  {m['clicks_success_after_retry']}")
    print(f"    - Timeouts de clic:  {m['clicks_timeout']}")
    print(f"    - Errores de clic:   {m['clicks_error']}")
    print(f"  Reintentos cabecera tarjeta: {m['header_card_retries']}")
    print(f"  Reintentos cabecera check:   {m['header_check_retries']}")
    print()

    if m["response_times"]:
        rts = [t for _, t in m["response_times"]]
        print(f"  Tiempos de respuesta:")
        print(f"    - Muestras:          {len(rts)}")
        print(f"    - Promedio:          {sum(rts)/len(rts):.2f}s")
        print(f"    - Mínimo:            {min(rts):.2f}s")
        print(f"    - Máximo:            {max(rts):.2f}s")
        print()

    if m["purchase_times"]:
        print(f"  Compras individuales:")
        for item_id, secs, result in m["purchase_times"]:
            symbol = "✅" if result == "ok" else "✗"
            print(f"    {symbol} {item_id:<20} {secs:>7.2f}s  [{result}]")
        print()

    if m["page_times"]:
        print(f"  Tiempos por página:")
        for page_num, secs in m["page_times"]:
            print(f"    Página {page_num:<3} {secs:>7.2f}s")
        print()

    print("=" * 72 + "\n")

# ============================================================
# POLLING ENGINE
# ============================================================

def _snapshot(msg):
    btns = []
    if msg.buttons:
        for row in msg.buttons:
            for b in row:
                btns.append(b.text)
    return (msg.text or "", tuple(btns))

async def get_baseline():
    messages = await client.get_messages(BOT, limit=3)
    for m in messages:
        if not m.out:
            return m.id, _snapshot(m)
    return 0, ("", tuple())

async def wait_for_response(baseline_id, baseline_sig, timeout=TIMEOUT, attempt=1):
    t0 = time.monotonic()
    deadline = t0 + timeout
    poll_count = 0
    while time.monotonic() < deadline:
        poll_count += 1
        try:
            messages = await client.get_messages(BOT, limit=3)
        except Exception as e:
            log(f"   [poll] Error: {e}")
            await asyncio.sleep(1)
            continue
        for m in messages:
            if m.out:
                continue
            if m.id > baseline_id:
                elapsed = time.monotonic() - t0
                log(f"   [poll] Nuevo mensaje id={m.id} (espera={elapsed:.2f}s, polls={poll_count})")
                METRICS["response_times"].append(("new_msg", elapsed))
                return m
            if m.id == baseline_id and baseline_sig is not None:
                sig = _snapshot(m)
                if sig != baseline_sig:
                    elapsed = time.monotonic() - t0
                    log(f"   [poll] Mensaje editado id={m.id} (espera={elapsed:.2f}s, polls={poll_count})")
                    METRICS["response_times"].append(("edited", elapsed))
                    return m
        await asyncio.sleep(POLL_INTERVAL)
    log(f"   [poll] ⏱ TIMEOUT tras {timeout}s (polls={poll_count})")
    return None

async def click_and_wait_with_retry(message, text, timeout=TIMEOUT, max_retries=MAX_RETRIES):
    METRICS["clicks_total"] += 1
    for attempt in range(1, max_retries + 1):
        baseline_id, baseline_sig = await get_baseline()
        log(f"   [click] Intento {attempt}/{max_retries} para '{text[:50]}...'")
        click_task = asyncio.create_task(message.click(text=text))
        try:
            await asyncio.wait_for(click_task, timeout=CLICK_TIMEOUT)
        except asyncio.TimeoutError:
            METRICS["clicks_timeout"] += 1
            log(f"   [click] ⏱ Timeout de clic (>{CLICK_TIMEOUT}s, intento {attempt})")
        except Exception as e:
            METRICS["clicks_error"] += 1
            log(f"   [click] ✗ Error: {e!r} (intento {attempt})")
        response = await wait_for_response(baseline_id, baseline_sig, timeout, attempt)
        if response is not None:
            if attempt == 1:
                METRICS["clicks_success_first_try"] += 1
            else:
                METRICS["clicks_success_after_retry"] += 1
            return response
        if attempt < max_retries:
            log(f"   [reintento] Esperando {RETRY_SLEEP}s...")
            await asyncio.sleep(RETRY_SLEEP)
    log(f"   [click] ✗ Fallaron todos los intentos.")
    return None

async def send_and_wait(text, timeout=TIMEOUT):
    baseline_id, baseline_sig = await get_baseline()
    await client.send_message(BOT, text)
    return await wait_for_response(baseline_id, baseline_sig, timeout)

# ============================================================
# UTILIDADES
# ============================================================

def _dump_buttons(message):
    if message.buttons:
        for r_i, row in enumerate(message.buttons):
            for b in row:
                log(f"   [{r_i}] {b.text!r}")
    else:
        log(f"   (sin botones) Texto: {(message.text or '')[:120]!r}")

def load_products():
    products = []
    log("Cargando productos.txt...")
    with open(PRODUCTOS_FILE, "r", encoding="utf-8") as f:
        content = f.read()
        log(f"[DEBUG] Contenido (primeros 200 chars):\n{content[:200]}")
        f.seek(0)
        for line_number, line in enumerate(f, 1):
            product_id = line.strip()
            if not product_id:
                continue
            products.append({"id": product_id, "priority": line_number})
    log(f"[DEBUG] IDs cargados: {len(products)} | primeros 10: {[p['id'] for p in products[:10]]}")
    return products

def print_message(message):
    log("=" * 60)
    log(f"ID: {message.id}")
    log("TEXTO:")
    log(message.text or "(sin texto)")
    if message.buttons:
        log("BOTONES:")
        for row_index, row in enumerate(message.buttons):
            for column_index, button in enumerate(row):
                log(f"[{row_index},{column_index}] {button.text}")
    log("=" * 60)

def get_items(message):
    items = []
    if not message.buttons:
        return items
    for row in message.buttons:
        for button in row:
            if "|" in button.text:
                items.append(button.text)
    return items

async def find_button(message, text):
    if not message.buttons:
        return None
    for row in message.buttons:
        for button in row:
            if button.text.strip().lower() == text.strip().lower():
                return button
    return None

async def find_check_button(message):
    if not message.buttons:
        return None
    for row in message.buttons:
        for button in row:
            if "check" in button.text.lower():
                return button
    return None

def extract_id(item_text):
    parts = item_text.split("|")
    if len(parts) < 2:
        return None
    return parts[0].strip()

def extract_price(item_text):
    parts = item_text.split("|")
    if len(parts) < 2:
        return None
    price_text = parts[1].strip()
    price_text = price_text.replace("💵", "").replace("$", "").replace("USD", "").strip()
    try:
        return float(price_text.replace(",", "."))
    except ValueError:
        return None

# ============================================================
# FILTRO DE ARTÍCULOS
# ============================================================

def filter_page_items(items, products, page_num):
    product_ids = {p["id"]: p["priority"] for p in products}
    valid = []
    log(f"   [debug] Analizando {len(items)} artículos de la página {page_num}...")
    for item in items:
        METRICS["items_analyzed"] += 1
        item_id = extract_id(item)
        price = extract_price(item)
        if item_id is None or price is None:
            log(f"   [debug] Pág {page_num} | ilegible | ✗ RECHAZADO: {item!r}")
            continue
        if item_id not in product_ids:
            log(f"   [debug] Pág {page_num} | {item_id} | ${price} | ✗ NO en productos.txt")
            continue
        if price > MAX_PRICE:
            log(f"   [debug] Pág {page_num} | {item_id} | ${price:.2f} | ✗ precio > {MAX_PRICE}")
            continue
        log(f"   [debug] Pág {page_num} | {item_id} | ${price:.2f} | ✓ VÁLIDO")
        METRICS["items_valid"] += 1
        valid.append({
            "id": item_id,
            "item": item,
            "price": price,
            "priority": product_ids[item_id],
            "page": page_num
        })
    seen = set()
    unique_list = []
    for rec in sorted(valid, key=lambda x: x["price"]):
        if rec["item"] not in seen:
            seen.add(rec["item"])
            unique_list.append(rec)
    unique_list.sort(key=lambda x: (x["priority"], x["price"]))
    return unique_list

# ============================================================
# NAVEGACIÓN
# ============================================================

async def navigate_to_page(current_page, target_page, message):
    while current_page < target_page:
        next_btn = await find_button(message, "next page ➡️")
        if not next_btn:
            log("No se encontró botón next page")
            return None
        t0 = time.perf_counter()
        new_msg = await click_and_wait_with_retry(message, next_btn.text, timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if not new_msg:
            log("No se recibió la página siguiente")
            return None
        message = new_msg
        current_page += 1
    while current_page > target_page:
        prev_btn = await find_button(message, "Previous")
        if not prev_btn:
            log("No se encontró botón Previous")
            return None
        t0 = time.perf_counter()
        new_msg = await click_and_wait_with_retry(message, prev_btn.text, timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if not new_msg:
            log("No se recibió la página anterior")
            return None
        message = new_msg
        current_page -= 1
    return message

# ============================================================
# COMPRA
# ============================================================

async def purchase_item(record, current_page, message):
    item_start = time.monotonic()

    log(f"\n>>> Comprando: {record['item']} (página {record['page']}, prioridad {record['priority']})")

    if current_page != record["page"]:
        log(f"Navegando de página {current_page} a {record['page']}...")
        message = await navigate_to_page(current_page, record["page"], message)
        if not message:
            METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "nav_fail"))
            return True, current_page, message
        current_page = record["page"]

    if not message.buttons:
        log("   ✗ Mensaje sin botones")
        METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "no_buttons"))
        return True, current_page, message
    found = False
    for row in message.buttons:
        for button in row:
            if button.text.strip() == record["item"].strip():
                found = True
    if not found:
        log("   ✗ El botón del artículo ya no existe")
        METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "button_gone"))
        return True, current_page, message

    for card_attempt in range(1, HEADER_ATTEMPTS + 1):
        if card_attempt > 1:
            METRICS["header_card_retries"] += 1
            log(f"   🔄 [TARJETA] Reintento {card_attempt - 1}/{HEADER_ATTEMPTS - 1}...")
            if not message.buttons:
                log("   ✗ Mensaje sin botones en reintento. Saltando.")
                METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "no_buttons_retry"))
                return True, current_page, message
            found = False
            for row in message.buttons:
                for button in row:
                    if button.text.strip() == record["item"].strip():
                        found = True
            if not found:
                log("   ✗ El botón del artículo ya no existe en reintento. Saltando.")
                METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "button_gone_retry"))
                return True, current_page, message

        log(f"   [TARJETA] Clic (intento {card_attempt}/{HEADER_ATTEMPTS})...")
        t0 = time.perf_counter()
        response = await click_and_wait_with_retry(message, record["item"], timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if response is None:
            log("   ✗ No hubo respuesta al clic en la tarjeta.")
            METRICS["purchases_no_response"] += 1
            METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "no_response"))
            return True, current_page, message

        used_buttons.add(record["item"])

        if response.text and INSUFFICIENT_MSG in response.text:
            log("   ✗ Saldo insuficiente.")
            METRICS["purchases_insufficient"] += 1
            METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "insufficient"))
            return True, current_page, message

        if response.text and CARD_HEADER_FAIL_MSG in response.text:
            log(f"   ⚠️ Error de cabecera tras TARJETA. Reintentando tarjeta...")
            await asyncio.sleep(2)
            continue

        log("   Respuesta del bot tras clic en tarjeta:")
        print_message(response)

        check_btn = await find_check_button(response)
        if not check_btn:
            log("   (No se encontró botón check, saltando)")
            METRICS["purchases_check_missing"] += 1
            METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "check_missing"))
            return True, current_page, message

        log(f"   -> [CHECK] Botón encontrado. Iniciando {CHECK_ATTEMPTS} intentos de check...")
        for check_attempt in range(1, CHECK_ATTEMPTS + 1):
            if check_attempt > 1:
                METRICS["header_check_retries"] += 1
                log(f"   🔄 [CHECK] Reintento {check_attempt - 1}/{CHECK_ATTEMPTS - 1}...")
                check_btn = await find_check_button(response)
                if not check_btn:
                    log("   ✗ El botón check ya no existe. Saltando tarjeta.")
                    break

            log(f"   [CHECK] Clic (intento {check_attempt}/{CHECK_ATTEMPTS})...")
            t0 = time.perf_counter()
            final = await click_and_wait_with_retry(response, check_btn.text, timeout=TIMEOUT)
            log(f"   (Respuesta final en {time.perf_counter() - t0:.2f}s)")

            if final is None:
                log("   ✗ No hubo respuesta al check.")
                continue

            final_text = final.text or ""

            if CARD_HEADER_FAIL_MSG in final_text:
                log(f"   ⚠️ Error de cabecera tras CHECK. Reintentando check...")
                await asyncio.sleep(2)
                continue

            if INSUFFICIENT_MSG in final_text:
                log("   ✗ Saldo insuficiente tras check.")
                METRICS["purchases_insufficient"] += 1
                METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "insufficient_check"))
                return True, current_page, message

            if "Order failed" in final_text:
                log("   ✗ Order failed.")
                METRICS["purchases_order_failed"] += 1
                METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "order_failed"))
                return True, current_page, message

            log("   ✅ COMPRA CONFIRMADA:")
            print_message(final)
            METRICS["purchases_ok"] += 1
            METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "ok"))
            return True, current_page, message

        log(f"   ✗ Check agotó sus {CHECK_ATTEMPTS} intentos. Reintentando tarjeta desde cero...")
        await asyncio.sleep(2)
        continue

    log(f"   ✗ Tarjeta agotó sus {HEADER_ATTEMPTS} intentos. Saltando artículo.")
    METRICS["purchases_header_fail"] += 1
    METRICS["purchase_times"].append((record["id"], time.monotonic() - item_start, "header_fail_exhausted"))
    return True, current_page, message

# ============================================================
# FLUJO INICIAL
# ============================================================

async def start_flow(max_retries=3):
    for attempt in range(1, max_retries + 1):
        log(f"=== Intento {attempt}/{max_retries} ===")
        log("[1] Enviando /start...")
        t0 = time.perf_counter()
        message = await send_and_wait("/start", timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if not message:
            log("No se recibió respuesta a /start.")
            await asyncio.sleep(2)
            continue
        log("[2] Pulsando Country...")
        button = await find_button(message, "Country")
        if not button:
            log("No se encontró 'Country'.")
            _dump_buttons(message)
            await asyncio.sleep(2)
            continue
        t0 = time.perf_counter()
        message = await click_and_wait_with_retry(message, button.text, timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if not message:
            await asyncio.sleep(2)
            continue
        log("[3] Pulsando 5...")
        button = await find_button(message, "5")
        if not button:
            log("No se encontró '5'.")
            _dump_buttons(message)
            await asyncio.sleep(2)
            continue
        t0 = time.perf_counter()
        message = await click_and_wait_with_retry(message, button.text, timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if not message:
            await asyncio.sleep(2)
            continue
        log("[4] Pulsando COLOMBIA...")
        button = await find_button(message, "COLOMBIA")
        if not button:
            log("No se encontró COLOMBIA.")
            _dump_buttons(message)
            await asyncio.sleep(2)
            continue
        t0 = time.perf_counter()
        message = await click_and_wait_with_retry(message, button.text, timeout=TIMEOUT)
        log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
        if not message:
            await asyncio.sleep(2)
            continue
        return message
    return None

# ============================================================
# MAIN
# ============================================================

async def main(trigger_name):
    global refund_detected

    reset_metrics(trigger_name)
    log(f"\n>>> SCRIPT v8.8.7 - TRIGGER @{trigger_name} <<<")

    while True:
        used_buttons.clear()
        refund_detected = False

        products = load_products()
        if not products:
            log("⚠️ No se cargaron productos.")
            METRICS["run_end"] = time.monotonic()
            print_run_summary()
            return

        phase_t0 = time.monotonic()
        message = await start_flow(max_retries=3)
        METRICS["phase_times"]["start_flow"] = time.monotonic() - phase_t0
        if not message:
            log("No se pudo completar el flujo inicial.")
            METRICS["run_end"] = time.monotonic()
            print_run_summary()
            return
        print_message(message)

        current_page = 1
        total_bought = 0

        while True:
            page_t0 = time.monotonic()
            METRICS["pages_visited"] += 1
            log("=" * 60)
            log(f"PÁGINA {current_page}")
            log("=" * 60)
            items = get_items(message)
            log(f"Artículos en esta página: {len(items)}")
            for item in items:
                log(item)

            purchase_list = filter_page_items(items, products, current_page) if items else []

            if purchase_list:
                log(f"\nCompras en esta página ({len(purchase_list)}):")
                for idx, rec in enumerate(purchase_list, 1):
                    log(f"  {idx}. ID {rec['id']} | ${rec['price']:.2f} | Prioridad {rec['priority']}")
                log("-" * 60)
                log(f"COMPRANDO PÁGINA {current_page}")
                log("-" * 60)
                for rec in purchase_list:
                    success, current_page, message = await purchase_item(rec, current_page, message)
                    if not success:
                        log("⚠️ Error crítico en purchase_item")
                    total_bought += 1
            else:
                log("No hay artículos válidos en esta página.")

            METRICS["page_times"].append((current_page, time.monotonic() - page_t0))

            next_btn = await find_button(message, "next page ➡️")
            if not next_btn:
                log("No hay más páginas. Fin del recorrido.")
                break

            log("Pasando a la siguiente página...")
            t0 = time.perf_counter()
            new_msg = await click_and_wait_with_retry(message, next_btn.text, timeout=TIMEOUT)
            log(f"   (Respuesta en {time.perf_counter() - t0:.2f}s)")
            if not new_msg:
                log("No se recibió la siguiente página. Fin.")
                break
            message = new_msg
            current_page += 1
            if current_page > MAX_PAGES:
                log(f"Límite de {MAX_PAGES} páginas alcanzado.")
                break

        log("=" * 60)
        log(f"Recorrido completado - {total_bought} compras intentadas")
        log("=" * 60)

        if refund_detected:
            log("✅ Se detectó refund. Reiniciando proceso...")
            continue

        log("⏳ Esperando hasta 2 minutos por refunds...")
        try:
            await asyncio.wait_for(refund_event.wait(), timeout=120)
            log("✅ Refund detectado. Reiniciando...")
            refund_event.clear()
            continue
        except asyncio.TimeoutError:
            log("⏰ Tiempo agotado. Sin refunds.")
            break

    log(">>> Flujo finalizado. Esperando próximo trigger...")
    METRICS["run_end"] = time.monotonic()
    print_run_summary()

# ============================================================
# HANDLERS
# ============================================================

async def refund_handler(event):
    """Detecta mensajes de refund (aunque ya no reinicia, solo loguea)."""
    global refund_detected
    if BOT_ID is not None and event.sender_id != BOT_ID:
        return
    if event.message.out:
        return
    text = event.message.text or ""
    if "refund" in text.lower() and "account balance" in text.lower():
        METRICS["refunds_detected"] += 1
        log(f"\n💰 REFUND DETECTADO: {text[:200]}")
        refund_detected = True
        refund_event.set()

_is_running = False

async def trigger_flow(trigger_name):
    global _is_running, last_flow_start
    if _is_running:
        log(f">>> Ya hay una ejecución en curso. Ignorando trigger de @{trigger_name}. <<<")
        return
    # Cooldown anti-cadena
    elapsed = time.monotonic() - last_flow_start
    if elapsed < TRIGGER_COOLDOWN:
        log(f">>> Cooldown activo ({elapsed:.1f}s < {TRIGGER_COOLDOWN}s). Ignorando trigger de @{trigger_name}. <<<")
        return
    _is_running = True
    last_flow_start = time.monotonic()
    try:
        log("=" * 60)
        log(f">>> TRIGGER RECIBIDO de @{trigger_name} - INICIANDO <<<")
        log("=" * 60)
        await main(trigger_name)
    except Exception as e:
        log(f">>> ERROR durante la ejecución: {e!r} <<<")
    finally:
        _is_running = False
        log(f">>> Flujo terminado. Esperando próximo trigger... <<<")

async def bot_trigger_handler(event):
    """Cualquier mensaje del bot de compras dispara el flujo."""
    if event.message.out:
        return
    text = event.message.text or ""
    # Ignorar mensajes que claramente son respuestas operativas de nuestras acciones
    # (aunque durante el flujo el lock ya los ignora)
    log(f"   [bot-trigger] Mensaje recibido de {event.sender_id}: {text[:80]!r}")
    asyncio.create_task(trigger_flow(TRIGGER_USERNAME))

async def manual_trigger_handler(event):
    """Trigger manual: cuando TÚ te envías 'run_test' a Saved Messages."""
    if not event.message.out:
        return
    text = (event.message.text or "").strip().lower()
    if not text:
        return
    # Loguear cualquier mensaje saliente para diagnóstico
    log(f"   [diag] Mensaje saliente detectado: chat_id={event.chat_id} | texto={text[:60]!r}")
    if MANUAL_TRIGGER_WORD in text:
        log(f"   [trigger-manual] ✅ Palabra '{MANUAL_TRIGGER_WORD}' detectada. Disparando flujo...")
        asyncio.create_task(trigger_flow("MANUAL_TEST"))

# ============================================================
# ARRANQUE
# ============================================================

async def run_forever():
    global BOT_ID
    while True:
        try:
            if not SESSION_STRING:
                raise RuntimeError("TELEGRAM_SESSION no definida")
            await client.start()
            me = await client.get_me()
            if me is None:
                raise RuntimeError("Sesión no autorizada")

            if BOT_ID is None:
                try:
                    bot_entity = await client.get_entity(BOT)
                    BOT_ID = bot_entity.id
                    log(f">>> ID de {BOT} resuelto: {BOT_ID} <<<")
                except Exception as e:
                    log(f">>> No se pudo resolver ID de {BOT}: {e!r} <<<")

            client.remove_event_handler(bot_trigger_handler, events.NewMessage)
            client.remove_event_handler(refund_handler, events.NewMessage)
            client.remove_event_handler(manual_trigger_handler, events.NewMessage)

            # Trigger principal: cualquier mensaje del bot
            client.add_event_handler(bot_trigger_handler, events.NewMessage(from_users=BOT_ID))

            # Refund detector (para logging)
            client.add_event_handler(refund_handler, events.NewMessage())

            # Trigger manual (Saved Messages)
            client.add_event_handler(
                manual_trigger_handler,
                events.NewMessage(chats='me', outgoing=True)
            )

            log(">>> SERVICIO v8.8.7 ACTIVO (COL) - Bot: @KingKongccs2bot <<<")
            log(f">>> Logueado como: {me.first_name} (@{me.username}) <<<")
            log(f">>> Bot: {BOT} (ID: {BOT_ID}) <<<")
            log(f">>> Cualquier mensaje de {BOT} dispara el flujo (cooldown: {TRIGGER_COOLDOWN}s) <<<")
            log(f">>> Trigger MANUAL: envíate '{MANUAL_TRIGGER_WORD}' a Saved Messages <<<")
            log(f">>> Precio máx: ${MAX_PRICE} | Tarjeta: {HEADER_ATTEMPTS} | Check: {CHECK_ATTEMPTS} | Clic: {MAX_RETRIES}x cada {RETRY_SLEEP}s <<<")

            await client.run_until_disconnected()

        except Exception as e:
            log(f">>> CONEXIÓN CAÍDA: {e!r} <<<")
            log(">>> Reintentando en 15 segundos... <<<")
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(15)

log(">>> Iniciando servicio... <<<")
client.loop.run_until_complete(run_forever())
