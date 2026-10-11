# Scanner adapted from the uploaded tracker. No embedded credentials, bot or group.
import asyncio
import itertools
import json
import time
from pathlib import Path
from telethon import TelegramClient, events
from telethon import helpers as tl_helpers
from telethon.extensions import markdown as tl_markdown
from telethon.tl.types import MessageEntityCustomEmoji
from telethon.tl.functions.messages import SearchCustomEmojiRequest
from telethon.tl.custom import Button
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.functions.users import GetRequirementsToContactRequest, GetFullUserRequest
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError, SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon.tl.types import RequirementToContactPaidMessages
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest, GetSavedStarGiftsRequest
GIFT_NAME_WHITELIST = {'Heart Locket', "Durov's Cap", 'Scared Cat', 'Precious Peach', "Khabib's Papakha", 'Heroic Helmet', 'Loot Bag', 'Astral Shard', 'Mighty Arm', 'Nail Bracelet', 'UFC box', 'Westside Sign', "Durov's Glasses", "Durov's Coat", 'Mini Oscar', 'Ion Gem', 'Perfume Bottle', "Durov's Boots", 'Gem Signet', 'Artisan Brick', 'Low Rider', 'Magic Potion', 'Swiss Watch', 'Gravestone', 'Sharp Tongue', 'Kissed Frog', 'Bonded Ring', 'Neko Helmet', 'Toy Bear', 'Vintage Cigar', 'Genie Lamp', 'Voodoo Doll', 'Signet Ring', 'Diamond Ring', 'Bling Binky', 'Electric Skull', 'Rare Bird', 'Eternal Rose', 'Coffin', 'Cupid Charm', 'Sky Stilettos', 'UFC Strike', 'Trapped Heart', 'Ionic Dryer', 'Snoop Cigar', 'Love Potion', 'Mad Pumpkin', 'Flying Broom', 'Record Player', 'Crystal Ball', 'Valentine Box'}
TRACK_ALL_COLLECTIONS = True
NUM_NEWEST_COLLECTIONS = 2
MANUAL_GIFT_IDS: list[int] = []
POLL_INTERVAL = 60
SEND_DELAY_SECONDS = 5.5
REQUEST_DELAY = 1.5
LIMIT_PER_GIFT = 30
OWNER_INFO_BATCH_SIZE = 10
OWNER_INFO_BATCH_DELAY = 1.0
MIN_PRICE_STARS = None
MAX_PRICE_STARS = None
ONLY_REGULAR_NAMES = False
NAME_BLOCKLIST_KEYWORDS = ['shop', 'market', 'магазин', 'опт', 'sell', 'buy', 'продажа', 'продам', 'official', 'официал', 'store', 'trade', 'trading', 'exchange', 'обмен', 'bot', 'бот', 'admin', 'админ', 'manager', 'менеджер', 'deal', 'deals', 'nft', 'gift shop', 'reseller', 'перекуп']
EXCLUDE_PAID_MESSAGE_SELLERS = True
MAX_SELLER_LEVEL = 1
VALUE_USD_DIVISOR = 100

def format_last_seen(status) -> str:
    """Переводит статус "онлайн" пользователя в человекочитаемый текст.
    Telegram по соображениям приватности часто отдаёт только грубую
    категорию (недавно/на этой неделе/в этом месяце), а не точное время."""
    type_name = type(status).__name__
    mapping = {'UserStatusOnline': 'В сети', 'UserStatusRecently': 'Недавно', 'UserStatusLastWeek': 'На этой неделе', 'UserStatusLastMonth': 'В этом месяце', 'UserStatusOffline': 'Недавно', 'UserStatusEmpty': 'Скрыт'}
    return mapping.get(type_name, 'Неизвестно')

async def count_unique_gifts_in_profile(client: TelegramClient, input_user) -> int:
    """Считает СКОЛЬКО ИМЕННО NFT (коллекционных, StarGiftUnique) подарков
    на профиле, а не общее число подарков (stargifts_count из getFullUser
    включает и обычные, не-NFT подарки тоже — это не то, что нужно).
    Ограничено MAX_GIFT_COUNT_PAGES страницами по 100 штук — для очень
    больших профилей (условно 500+ подарков) число может быть чуть занижено,
    но для подавляющего большинства аккаунтов будет точным."""
    MAX_GIFT_COUNT_PAGES = 5
    count = 0
    offset = ''
    for _ in range(MAX_GIFT_COUNT_PAGES):
        result = await client(GetSavedStarGiftsRequest(peer=input_user, offset=offset, limit=100))
        for item in getattr(result, 'gifts', []):
            if type(getattr(item, 'gift', None)).__name__ == 'StarGiftUnique':
                count += 1
        offset = getattr(result, 'next_offset', None)
        if not offset:
            break
    return count

async def _resolve_single_owner(client: TelegramClient, uid: int) -> tuple:
    """Достаёт данные ОДНОГО продавца. Возвращает (uid, info_dict, input_user|None)."""
    username = None
    premium = False
    last_seen_text = 'Неизвестно'
    try:
        entity = await client.get_entity(uid)
        username = getattr(entity, 'username', None)
        premium = bool(getattr(entity, 'premium', False))
        last_seen_text = format_last_seen(getattr(entity, 'status', None))
    except FloodWaitError:
        raise
    except Exception as e:
        print(f'[!] Не удалось получить данные продавца id={uid}: {e}')
    gifts_count = None
    level = None
    about = ''
    input_user = None
    try:
        input_user = await client.get_input_entity(uid)
        full = await client(GetFullUserRequest(id=input_user))
        full_user = getattr(full, 'full_user', None)
        rating = getattr(full_user, 'stars_rating', None)
        level = getattr(rating, 'level', None)
        about = getattr(full_user, 'about', None) or ''
    except FloodWaitError:
        raise
    except Exception as e:
        print(f'[!] Не удалось получить доп. данные (уровень/био) продавца id={uid}: {e}')
    if input_user is not None:
        try:
            gifts_count = await count_unique_gifts_in_profile(client, input_user)
        except FloodWaitError:
            raise
        except Exception as e:
            print(f'[!] Не удалось посчитать NFT-подарки продавца id={uid}: {e}')
    info = {'username': username, 'paid': None, 'premium': premium, 'last_seen_text': last_seen_text, 'gifts_count': gifts_count, 'level': level, 'about': about}
    return (uid, info, input_user)

async def resolve_owner_info(client: TelegramClient, new_listings: list) -> dict:
    """Для каждого уникального продавца из new_listings пачкой (параллельно,
    порциями по OWNER_INFO_BATCH_SIZE) достаёт:
    - username (для нормальной ссылки t.me/username вместо ненадёжной
      tg://user?id=, которая открывается не у всех — она требует, чтобы
      кликающий уже "видел" этого пользователя в своём Telegram)
    - признак "платные сообщения" (через users.getRequirementsToContact)
    - Premium-статус, "был онлайн", число подарков в профиле и звёздный
      уровень (через users.getFullUser — отдельный запрос на каждого)

    Возвращает {user_id: {...}}.
    Если продавца технически не получилось проверить — он просто не
    попадёт в словарь, в консоль печатается предупреждение."""
    user_ids = []
    seen_ids = set()
    for listing in new_listings:
        owner_id = listing.get('owner_id')
        uid = getattr(owner_id, 'user_id', None)
        if uid and uid not in seen_ids:
            seen_ids.add(uid)
            user_ids.append(uid)
    info: dict = {}
    if not user_ids:
        return info
    print(f'  Проверяю данные {len(user_ids)} продавцов...')
    input_users = []
    resolved_order = []
    total_batches = (len(user_ids) + OWNER_INFO_BATCH_SIZE - 1) // OWNER_INFO_BATCH_SIZE
    for batch_num, i in enumerate(range(0, len(user_ids), OWNER_INFO_BATCH_SIZE), start=1):
        batch = user_ids[i:i + OWNER_INFO_BATCH_SIZE]
        print(f'  ...продавцы: пачка {batch_num}/{total_batches} ({i + len(batch)}/{len(user_ids)})', end='\r')
        results = await asyncio.gather(*(_resolve_single_owner(client, uid) for uid in batch))
        for uid, owner_info, input_user in results:
            info[uid] = owner_info
            if input_user is not None:
                input_users.append(input_user)
                resolved_order.append(uid)
        if i + OWNER_INFO_BATCH_SIZE < len(user_ids):
            await asyncio.sleep(OWNER_INFO_BATCH_DELAY)
    print()
    if input_users:
        try:
            result = await client(GetRequirementsToContactRequest(id=input_users))
            for uid, requirement in zip(resolved_order, result):
                info[uid]['paid'] = isinstance(requirement, RequirementToContactPaidMessages)
        except FloodWaitError:
            raise
        except Exception as e:
            print(f'[!] Ошибка users.getRequirementsToContact: {e}')
    return info

async def get_newest_gift_ids(client: TelegramClient, count: int) -> list[int]:
    """Берёт каталог коллекционных подарков и возвращает ID `count` самых
    недавно вышедших (по полю first_sale_date — дата начала продаж)."""
    result = await client(GetStarGiftsRequest(hash=0))
    collectibles = []
    for g in getattr(result, 'gifts', []):
        if not getattr(g, 'limited', False):
            continue
        first_sale = getattr(g, 'first_sale_date', None) or 0
        collectibles.append((first_sale, g.id, getattr(g, 'title', str(g.id))))
    collectibles.sort(key=lambda x: x[0], reverse=True)
    top = collectibles[:count]
    for first_sale, gid, title in top:
        print(f'  · отслеживаем: {title} (id={gid})')
    return [gid for _, gid, _ in top]

def _normalize_gift_name(name: str) -> str:
    """Приводит название к единому виду для сравнения: нижний регистр,
    обычный апостроф вместо "красивого" (’ -> '), без лишних пробелов."""
    return (name or '').strip().lower().replace('’', "'")

async def get_gift_ids_by_names(client: TelegramClient, whitelist: set) -> list[int]:
    """Берёт каталог подарков и возвращает ID только тех коллекций, чьи
    названия совпадают (без учёта регистра/апострофов) с whitelist."""
    result = await client(GetStarGiftsRequest(hash=0))
    normalized_whitelist = {_normalize_gift_name(n) for n in whitelist}
    found_names = set()
    ids = []
    for g in getattr(result, 'gifts', []):
        title = getattr(g, 'title', '')
        norm_title = _normalize_gift_name(title)
        if norm_title in normalized_whitelist:
            ids.append(g.id)
            found_names.add(norm_title)
            print(f'  · найден: {title} (id={g.id})')
    missing = normalized_whitelist - found_names
    if missing:
        print(f'[!] Не найдены в каталоге Telegram ({len(missing)} шт.): {sorted(missing)}')
    return ids

async def get_all_gift_ids(client: TelegramClient) -> list[int]:
    """Берёт каталог и возвращает ID ВСЕХ коллекционных (limited) подарков —
    то есть весь маркетплейс перепродажи целиком."""
    result = await client(GetStarGiftsRequest(hash=0))
    all_gifts = getattr(result, 'gifts', [])
    print(f'  · всего подарков в ответе (до фильтра limited): {len(all_gifts)}, тип ответа: {type(result).__name__}')
    if all_gifts:
        sample = all_gifts[0]
        print(f"  · пример первого подарка: id={getattr(sample, 'id', '?')}, limited={getattr(sample, 'limited', '?')}, title={getattr(sample, 'title', '?')}")
    ids = []
    for g in all_gifts:
        if getattr(g, 'limited', False):
            ids.append(g.id)
    print(f'  · найдено коллекций в каталоге: {len(ids)}')
    return ids

def looks_like_regular_name(name: str) -> bool:
    """Грубая эвристика: похоже ли имя на обычное человеческое, а не на
    магазин/перекупа. НЕ идеальна — см. комментарий у ONLY_REGULAR_NAMES.

    Красивые стилизованные шрифты (типа "𝓶𝓪𝓼𝓱𝓪", "Ⓜ𝕒𝕤𝕙𝕒" и т.п.) — это
    обычные люди, просто с необычным юникод-шрифтом в имени. Такие символы
    часто не имеют привычного строчный/заглавный регистра в Python, из-за
    чего проверка "капс ли это" может ложно принять их за капс — поэтому
    эта проверка применяется ТОЛЬКО к обычным ASCII/кириллическим буквам."""
    if not name or name == 'скрыт':
        return False
    lowered = name.lower()
    for kw in NAME_BLOCKLIST_KEYWORDS:
        if kw in lowered:
            return False
    digit_count = sum((ch.isdigit() for ch in name))
    if digit_count >= 3:
        return False
    plain_letters = [ch for ch in name if ch.isalpha() and ch.lower() != ch.upper()]
    if plain_letters and name == name.upper() and (len(plain_letters) > 3):
        return False
    return True

def extract_model_rarity_permille(attributes):
    """Ищет атрибут модели среди attributes подарка, возвращает редкость
    в промилле (0-1000) или None, если не нашли/поле отсутствует.
    Названия полей в схеме Telegram менялись между слоями API, поэтому
    проверяем оба варианта: rarity_permille (старый) и rarity.permille (новый)."""
    if not attributes:
        return None
    for attr in attributes:
        if type(attr).__name__ != 'StarGiftAttributeModel':
            continue
        permille = getattr(attr, 'rarity_permille', None)
        if permille is not None:
            return permille
        rarity_obj = getattr(attr, 'rarity', None)
        permille = getattr(rarity_obj, 'permille', None)
        if permille is not None:
            return permille
        return None
    return None

def extract_model_name(attributes) -> str:
    """Ищет атрибут модели среди attributes подарка, возвращает её название."""
    if not attributes:
        return ''
    for attr in attributes:
        if type(attr).__name__ != 'StarGiftAttributeModel':
            continue
        return getattr(attr, 'name', '') or ''
    return ''

def extract_backdrop_name(attributes) -> str:
    """Ищет атрибут фона (backdrop) среди attributes подарка, возвращает имя."""
    if not attributes:
        return ''
    for attr in attributes:
        if type(attr).__name__ != 'StarGiftAttributeBackdrop':
            continue
        return getattr(attr, 'name', '') or ''
    return ''

def looks_chinese_name(name: str) -> bool:
    """Грубая эвристика: есть ли иероглифы/письменность Восточной Азии —
    китайские (CJK Unified Ideographs), японские (хирагана/катакана) или
    корейские (хангыль). Это определение ПО ПИСЬМЕННОСТИ текста, а не
    точное определение национальности — метод приблизительный."""
    if not name:
        return False
    for ch in name:
        code = ord(ch)
        if 19968 <= code <= 40959 or 13312 <= code <= 19903 or 12352 <= code <= 12447 or (12448 <= code <= 12543) or (44032 <= code <= 55203):
            return True
    return False

def looks_cjk_anywhere(*texts: str) -> bool:
    """True, если хотя бы в одном из переданных текстов (имя, био и т.п.)
    есть письменность Восточной Азии — см. looks_chinese_name."""
    return any((looks_chinese_name(t) for t in texts if t))

def format_price(resell_amount) -> str:
    """Цена лота может быть выставлена в звёздах (Stars/XTR) ИЛИ в TON —
    это два разных типа объекта в ответе API, у них нет общего поля
    "currency", поэтому различаем по имени класса."""
    if not resell_amount:
        return '—'
    parts = []
    for a in resell_amount:
        type_name = type(a).__name__
        if type_name == 'StarsTonAmount':
            ton_amount = getattr(a, 'amount', 0) / 1000000000
            parts.append(f'{ton_amount:.4f} TON')
        else:
            stars = getattr(a, 'amount', '?')
            parts.append(f'{stars} XTR')
    return ' / '.join(parts)

async def check_gift(client: TelegramClient, gift_id: int, seen: set, new_listings: list, stats: dict) -> None:
    try:
        result = await client(GetResaleStarGiftsRequest(gift_id=gift_id, offset='', limit=LIMIT_PER_GIFT, sort_by_num=False, sort_by_price=False))
    except FloodWaitError:
        raise
    except Exception as e:
        print(f'[!] Ошибка запроса для gift_id={gift_id}: {e}')
        stats['errors'] += 1
        return
    total_in_response = len(getattr(result, 'gifts', []))
    stats['total_seen_in_responses'] += total_in_response
    for gift in getattr(result, 'gifts', []):
        slug = getattr(gift, 'slug', None)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        stats['new_slugs'] += 1
        price_str = format_price(getattr(gift, 'resell_amount', None))
        owner_name = getattr(gift, 'owner_name', None) or 'скрыт'
        owner_id = getattr(gift, 'owner_id', None)
        title = getattr(gift, 'title', 'Подарок')
        num = getattr(gift, 'num', '?')
        attributes = getattr(gift, 'attributes', None)
        raw_value_usd = getattr(gift, 'value_usd_amount', None)
        value_usd = raw_value_usd / VALUE_USD_DIVISOR if raw_value_usd is not None else None
        if ONLY_REGULAR_NAMES and (not looks_like_regular_name(owner_name)):
            stats['filtered_by_name'] += 1
            stats['filtered_names_sample'].append(owner_name)
            continue
        new_listings.append({'title': title, 'num': num, 'slug': slug, 'price': price_str, 'owner_name': owner_name, 'owner_id': owner_id, 'model_rarity_permille': extract_model_rarity_permille(attributes), 'model_name': extract_model_name(attributes), 'backdrop_name': extract_backdrop_name(attributes), 'value_usd': value_usd})
