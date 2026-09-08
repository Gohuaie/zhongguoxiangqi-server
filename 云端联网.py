import asyncio
import copy
import json
import logging
import os
import secrets
import string
import time
from http import HTTPStatus
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import websockets
from websockets.exceptions import ConnectionClosed


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("xiangqi-server")

ROOMS = {}
CLIENTS = {}
DISCONNECT_GRACE_SECONDS = max(5, int(os.environ.get("DISCONNECT_GRACE_SECONDS", "30")))
SEND_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("SEND_TIMEOUT_SECONDS", "3")))
CHAT_SECURITY_TIMEOUT_SECONDS = max(2.0, float(os.environ.get("CHAT_SECURITY_TIMEOUT_SECONDS", "7")))
MAX_MESSAGE_SIZE = 64 * 1024
WECHAT_APPID = os.environ.get("WECHAT_APPID", "").strip()
WECHAT_APPSECRET = os.environ.get("WECHAT_APPSECRET", "").strip()
WECHAT_CONTENT_SECURITY_ENABLED = os.environ.get("WECHAT_CONTENT_SECURITY_ENABLED", "1") != "0"
WECHAT_SECURITY_FAIL_CLOSED = os.environ.get("WECHAT_SECURITY_FAIL_CLOSED", "1") != "0"
WECHAT_API_BASE = "https://api.weixin.qq.com"
WECHAT_TOKEN_CACHE = {"value": "", "expires_at": 0.0}
WECHAT_TOKEN_LOCK = asyncio.Lock()
BACKGROUND_TASKS = set()
DEFAULT_FORBIDDEN_WORDS = {
    "傻逼", "操你妈", "草你妈", "妈的", "去死", "色情", "赌博", "毒品",
    "加微信", "微信号", "qq群", "代充", "fuck", "shit",
}


def load_forbidden_words():
    words = set(DEFAULT_FORBIDDEN_WORDS)
    word_file = Path(__file__).with_name("违禁词.txt")
    if word_file.exists():
        try:
            words.update(
                line.strip()
                for line in word_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        except OSError:
            LOGGER.exception("读取违禁词文件失败")
    words.update(item.strip() for item in os.environ.get("BLOCKED_WORDS", "").split(",") if item.strip())
    return sorted(words, key=len, reverse=True)


FORBIDDEN_WORDS = load_forbidden_words()
TRIE_END = object()


def build_forbidden_trie(words):
    root = {}
    for word in words:
        node = root
        for character in word.lower():
            node = node.setdefault(character, {})
        node[TRIE_END] = True
    return root


FORBIDDEN_TRIE = build_forbidden_trie(FORBIDDEN_WORDS)


def request_wechat_json(path, query=None, payload=None):
    url = WECHAT_API_BASE + path
    if query:
        url += "?" + urllib_parse.urlencode(query)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    request = urllib_request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib_request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib_error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("微信内容安全接口请求失败") from exc


async def get_wechat_access_token(force_refresh=False):
    if not WECHAT_APPID or not WECHAT_APPSECRET:
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    now = time.time()
    if not force_refresh and WECHAT_TOKEN_CACHE["value"] and WECHAT_TOKEN_CACHE["expires_at"] > now + 120:
        return WECHAT_TOKEN_CACHE["value"]
    async with WECHAT_TOKEN_LOCK:
        now = time.time()
        if not force_refresh and WECHAT_TOKEN_CACHE["value"] and WECHAT_TOKEN_CACHE["expires_at"] > now + 120:
            return WECHAT_TOKEN_CACHE["value"]
        result = await asyncio.to_thread(
            request_wechat_json,
            "/cgi-bin/token",
            {"grant_type": "client_credential", "appid": WECHAT_APPID, "secret": WECHAT_APPSECRET},
        )
        token = result.get("access_token")
        if not token:
            raise RuntimeError(f"获取微信 access_token 失败: {result.get('errcode', 'unknown')}")
        WECHAT_TOKEN_CACHE["value"] = token
        WECHAT_TOKEN_CACHE["expires_at"] = now + max(300, int(result.get("expires_in", 7200)))
        return token


async def exchange_wechat_code(code):
    if not WECHAT_APPID or not WECHAT_APPSECRET:
        raise RuntimeError("未配置微信小游戏身份参数")
    result = await asyncio.to_thread(
        request_wechat_json,
        "/sns/jscode2session",
        {"appid": WECHAT_APPID, "secret": WECHAT_APPSECRET, "js_code": code, "grant_type": "authorization_code"},
    )
    if not result.get("openid"):
        raise RuntimeError(f"微信登录凭证校验失败: {result.get('errcode', 'unknown')}")
    return result["openid"]


async def check_wechat_text(content, client, scene):
    """返回 (是否通过, 面向用户的提示)。未通过的内容绝不广播或入库。"""
    if not WECHAT_CONTENT_SECURITY_ENABLED:
        return True, ""
    try:
        token = await get_wechat_access_token()
        payload = {"content": content}
        openid = client.get("openid")
        if openid:
            payload.update({"version": 2, "scene": scene, "openid": openid})
            if scene == 1:
                payload["nickname"] = content
        result = await asyncio.to_thread(
            request_wechat_json, "/wxa/msg_sec_check", {"access_token": token}, payload
        )
        if result.get("errcode") in (40001, 40014, 42001):
            token = await get_wechat_access_token(force_refresh=True)
            result = await asyncio.to_thread(
                request_wechat_json, "/wxa/msg_sec_check", {"access_token": token}, payload
            )
        if result.get("errcode") == 87014:
            return False, "内容未通过微信安全检测，请修改后重试"
        if result.get("errcode") != 0:
            raise RuntimeError(f"微信文本安全检测失败: {result.get('errcode', 'unknown')}")
        suggestion = (result.get("result") or {}).get("suggest", "pass")
        if suggestion == "pass":
            return True, ""
        LOGGER.warning(
            "微信内容安全拦截 scene=%s suggest=%s label=%s",
            scene,
            suggestion,
            (result.get("result") or {}).get("label", "unknown"),
        )
        if suggestion == "review":
            return False, "内容需要人工复核，暂时无法发布"
        return False, "内容未通过微信安全检测，请修改后重试"
    except Exception:
        LOGGER.exception("微信文本内容安全检测异常")
        if WECHAT_SECURITY_FAIL_CLOSED:
            return False, "内容安全服务暂时不可用，请稍后重试"
        return True, ""


async def check_wechat_image(image_bytes, filename="upload.jpg", content_type="image/jpeg"):
    """预留的 imgSecCheck 服务端入口；项目当前没有用户图片上传场景。"""
    if not WECHAT_CONTENT_SECURITY_ENABLED:
        return True
    token = await get_wechat_access_token()
    boundary = "----XiangqiSecurityBoundary"
    safe_filename = Path(filename).name.replace('"', "")
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"{safe_filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body = prefix + image_bytes + f"\r\n--{boundary}--\r\n".encode("ascii")
    url = WECHAT_API_BASE + "/wxa/img_sec_check?" + urllib_parse.urlencode({"access_token": token})
    request = urllib_request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    try:
        def perform_request():
            with urllib_request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        result = await asyncio.to_thread(perform_request)
        return result.get("errcode") == 0
    except Exception:
        LOGGER.exception("微信图片内容安全检测异常")
        return not WECHAT_SECURITY_FAIL_CLOSED

INITIAL_BOARD = [
    ["b_c", "b_m", "b_x", "b_s", "b_j", "b_s", "b_x", "b_m", "b_c"],
    [None] * 9,
    [None, "b_p", None, None, None, None, None, "b_p", None],
    ["b_z", None, "b_z", None, "b_z", None, "b_z", None, "b_z"],
    [None] * 9,
    [None] * 9,
    ["r_z", None, "r_z", None, "r_z", None, "r_z", None, "r_z"],
    [None, "r_p", None, None, None, None, None, "r_p", None],
    [None] * 9,
    ["r_c", "r_m", "r_x", "r_s", "r_j", "r_s", "r_x", "r_m", "r_c"],
]


def new_game_snapshot():
    return {
        "board": [row[:] for row in INITIAL_BOARD],
        "turn": "r",
        "lastMove": None,
        "capturedPieces": {"r": [], "b": []},
        "gameStatus": "playing",
        "winner": None,
        "endReason": "",
    }


def valid_board(board):
    return (
        isinstance(board, list)
        and len(board) == 10
        and all(isinstance(row, list) and len(row) == 9 for row in board)
    )


def sync_payload(room):
    snapshot = room["snapshot"]
    return {
        "type": "sync_board",
        "board": snapshot["board"],
        "turn": snapshot["turn"],
        "lastMove": snapshot["lastMove"],
        "capturedPieces": snapshot["capturedPieces"],
        "version": room.get("version", 0),
        "game_status": snapshot.get("gameStatus", "playing"),
        "winner": snapshot.get("winner"),
        "end_reason": snapshot.get("endReason", ""),
    }


def clean_text(value, limit):
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()[:limit]


def mask_forbidden_text(value):
    characters = list(str(value))
    changed = False
    start = 0
    while start < len(characters):
        node = FORBIDDEN_TRIE
        cursor = start
        longest_end = None
        while cursor < len(characters):
            node = node.get(characters[cursor].lower())
            if node is None:
                break
            cursor += 1
            if TRIE_END in node:
                longest_end = cursor
        if longest_end is None:
            start += 1
            continue
        characters[start:longest_end] = ["*"] * (longest_end - start)
        changed = True
        start = longest_end
    return "".join(characters), changed


def chat_role(client):
    if client.get("side") == "r":
        return "红方"
    if client.get("side") == "b":
        return "黑方"
    spectator_number = client.get("spectator_number")
    return f"观战者{spectator_number}" if spectator_number else "未选边"


def remember_chat(room, payload):
    room["chat_history"].append(payload)
    if len(room["chat_history"]) > 50:
        room["chat_history"] = room["chat_history"][-50:]


def run_in_background(coroutine):
    """运行不应阻塞走棋/心跳的慢任务，并保留引用以便记录异常。"""
    task = asyncio.create_task(coroutine)
    BACKGROUND_TASKS.add(task)

    def task_done(completed):
        BACKGROUND_TASKS.discard(completed)
        if not completed.cancelled() and completed.exception():
            LOGGER.error(
                "后台任务失败",
                exc_info=(type(completed.exception()), completed.exception(), completed.exception().__traceback__),
            )

    task.add_done_callback(task_done)
    return task


async def safe_send(websocket, payload):
    try:
        await asyncio.wait_for(
            websocket.send(json.dumps(payload, ensure_ascii=False)),
            timeout=SEND_TIMEOUT_SECONDS,
        )
        return True
    except asyncio.TimeoutError:
        LOGGER.warning("发送消息超时，关闭慢连接")
        run_in_background(websocket.close(code=1011, reason="发送超时，请重新连接"))
        return False
    except ConnectionClosed:
        return False
    except Exception:
        LOGGER.exception("发送消息失败")
        return False


def roles_status(room):
    return {side: room["roles"][side] is not None for side in ("r", "b")}


def game_started(room):
    return all(room["roles"][side] is not None for side in ("r", "b"))


def room_list_payload():
    rooms = [
        {
            "id": room_id,
            "count": len(room["players"]),
            "hasPwd": bool(room["pwd"]),
            "isPlaying": game_started(room),
        }
        for room_id, room in ROOMS.items()
    ]
    return {"type": "room_list", "rooms": rooms}


async def send_room_list(websocket):
    await safe_send(websocket, room_list_payload())


async def broadcast_lobby_list():
    recipients = [ws for ws, client in list(CLIENTS.items()) if not client["room_id"]]
    if recipients:
        payload = room_list_payload()
        await asyncio.gather(*(safe_send(ws, payload) for ws in recipients))


async def notify_room(room, payload, exclude=None):
    recipients = [ws for ws in list(room["players"]) if ws is not exclude]
    if recipients:
        await asyncio.gather(*(safe_send(ws, payload) for ws in recipients))


async def broadcast_room_info(room_id):
    room = ROOMS.get(room_id)
    if room:
        await notify_room(
            room,
            {"type": "room_info", "count": len(room["players"]), "roles": roles_status(room)},
        )


def cancel_disconnect_task(room, side):
    task = room["disconnect_tasks"].pop(side, None)
    if task and task is not asyncio.current_task():
        task.cancel()


async def expire_disconnected_role(room_id, side, old_websocket):
    try:
        await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        room = ROOMS.get(room_id)
        if not room or room["roles"].get(side) is not old_websocket:
            return
        room["roles"][side] = None
        room["role_sessions"][side] = None
        room["round_started"] = False
        room["disconnect_tasks"].pop(side, None)
        await notify_room(room, {"type": "opponent_left"})
        if not room["players"] and not any(room["roles"].values()):
            ROOMS.pop(room_id, None)
            LOGGER.info("清理空房间 %s", room_id)
        else:
            await broadcast_room_info(room_id)
        await broadcast_lobby_list()
    except asyncio.CancelledError:
        pass


async def expire_disconnected_spectator(room_id, session_id):
    try:
        await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        room = ROOMS.get(room_id)
        if not room:
            return
        room.get("spectator_sessions", {}).pop(session_id, None)
        room.get("spectator_disconnect_tasks", {}).pop(session_id, None)
    except asyncio.CancelledError:
        pass


async def detach_client(websocket, reserve_role=True):
    client = CLIENTS.pop(websocket, None)
    if not client:
        return
    room_id, side = client.get("room_id"), client.get("side")
    room = ROOMS.get(room_id)
    if not room:
        return

    room["players"].discard(websocket)
    owns_role = side in ("r", "b") and room["roles"].get(side) is websocket
    spectator_session = client.get("session_id") if client.get("spectator_number") else None
    if owns_role and reserve_role:
        cancel_disconnect_task(room, side)
        room["disconnect_tasks"][side] = asyncio.create_task(
            expire_disconnected_role(room_id, side, websocket)
        )
        await notify_room(
            room,
            {
                "type": "opponent_offline",
                "grace_seconds": DISCONNECT_GRACE_SECONDS,
                "role": chat_role(client),
                "nickname": client.get("nickname", "棋友"),
            },
        )
    elif owns_role:
        room["roles"][side] = None
        room["role_sessions"][side] = None
        room["round_started"] = False
        cancel_disconnect_task(room, side)
        await notify_room(room, {"type": "opponent_left"})

    if spectator_session:
        old_task = room.get("spectator_disconnect_tasks", {}).pop(spectator_session, None)
        if old_task:
            old_task.cancel()
        if reserve_role:
            room["spectator_disconnect_tasks"][spectator_session] = asyncio.create_task(
                expire_disconnected_spectator(room_id, spectator_session)
            )
        else:
            room["spectator_sessions"].pop(spectator_session, None)

    if not room["players"] and not any(room["roles"].values()):
        ROOMS.pop(room_id, None)
    else:
        await broadcast_room_info(room_id)
    await broadcast_lobby_list()


def new_room_id():
    while True:
        room_id = "".join(secrets.choice(string.digits) for _ in range(4))
        if room_id not in ROOMS:
            return room_id


def valid_move(message):
    try:
        points = (message["from"], message["to"])
        return all(
            isinstance(point, dict)
            and isinstance(point.get("r"), int)
            and isinstance(point.get("c"), int)
            and 0 <= point["r"] <= 9
            and 0 <= point["c"] <= 8
            for point in points
        )
    except (KeyError, TypeError):
        return False


async def request_board_sync(room, reconnecting_websocket):
    for side in ("r", "b"):
        player = room["roles"][side]
        if player and player is not reconnecting_websocket and player in room["players"]:
            await safe_send(player, {"type": "request_sync"})
            break


async def process_wechat_login(websocket, client, code):
    try:
        client["openid"] = await exchange_wechat_code(code)
        if CLIENTS.get(websocket) is client:
            await safe_send(websocket, {"type": "wechat_login_success"})
    except Exception:
        LOGGER.exception("微信小游戏登录凭证校验失败")
        if CLIENTS.get(websocket) is client:
            await safe_send(websocket, {"type": "wechat_login_failed", "msg": "身份验证失败，将使用基础内容检测"})


async def process_nickname(websocket, client, candidate):
    login_task = client.get("login_task")
    if login_task and not login_task.done():
        try:
            await asyncio.shield(login_task)
        except Exception:
            pass
    approved, reason = await check_wechat_text(candidate, client, scene=1)
    if CLIENTS.get(websocket) is not client:
        return
    if not approved:
        return await safe_send(websocket, {"type": "nickname_rejected", "msg": reason})
    client["nickname"] = candidate
    return await safe_send(websocket, {"type": "nickname_accepted", "nickname": candidate})


async def process_chat(websocket, client, room_id, message, message_id):
    try:
        room = ROOMS.get(room_id)
        if not room or CLIENTS.get(websocket) is not client or client.get("room_id") != room_id:
            return

        # 本地词库先屏蔽，微信官方安全检测仍为最终发布门槛。
        message, filtered = mask_forbidden_text(message)
        try:
            approved, reason = await asyncio.wait_for(
                check_wechat_text(message, client, scene=2),
                timeout=CHAT_SECURITY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            approved, reason = False, "内容安全检测响应超时，请稍后重试"

        room = ROOMS.get(room_id)
        if not room or CLIENTS.get(websocket) is not client or client.get("room_id") != room_id:
            return
        if not approved:
            return await safe_send(
                websocket,
                {"type": "content_rejected", "scope": "chat", "msg": reason, "message_id": message_id},
            )
        if filtered:
            await safe_send(
                websocket,
                {"type": "content_filtered", "scope": "chat", "msg": "消息中的违禁词已自动屏蔽", "message_id": message_id},
            )
        payload = {
            "type": "chat",
            "msg": message,
            "nickname": client.get("nickname", "棋友"),
            "role": chat_role(client),
            "filtered": filtered,
            "message_id": message_id,
        }
        remember_chat(room, payload)
        await notify_room(room, payload)
    finally:
        client.get("pending_chat_ids", set()).discard(message_id)


async def handle_message(websocket, message):
    data = json.loads(message)
    if isinstance(data, dict) and isinstance(data.get("data"), str):
        data = json.loads(data["data"])
    if not isinstance(data, dict):
        return await safe_send(websocket, {"type": "error", "msg": "消息格式无效"})

    message_type = data.get("type")
    client = CLIENTS.get(websocket)
    if not client:
        return

    if message_type == "wechat_login":
        code = clean_text(data.get("code"), 128)
        if not code:
            return await safe_send(websocket, {"type": "wechat_login_failed", "msg": "微信登录凭证为空"})
        client["login_task"] = run_in_background(process_wechat_login(websocket, client, code))
        return

    if message_type == "set_nickname":
        candidate = clean_text(data.get("nickname"), 10) or "棋友"
        _, blocked = mask_forbidden_text(candidate)
        if blocked:
            return await safe_send(websocket, {"type": "nickname_rejected", "msg": "昵称含有违禁词，请重新设置"})
        previous_task = client.get("nickname_task")
        if previous_task and not previous_task.done():
            previous_task.cancel()
        client["nickname_task"] = run_in_background(process_nickname(websocket, client, candidate))
        return

    if message_type == "set_cheat_mode":
        client["cheat_mode"] = data.get("enabled") is True
        await safe_send(websocket, {"type": "cheat_mode", "enabled": client["cheat_mode"]})
        room = ROOMS.get(client.get("room_id"))
        if room and client.get("side") in ("r", "b") and data.get("announce") is True:
            status = "开启" if client["cheat_mode"] else "关闭"
            notice = {
                "type": "system_chat",
                "msg": f"【{chat_role(client)}】{client.get('nickname', '棋友')}已{status}作弊模式",
            }
            remember_chat(room, notice)
            await notify_room(room, notice)
        return

    if message_type == "ping":
        return await safe_send(websocket, {"type": "pong"})
    if message_type == "get_rooms":
        return await send_room_list(websocket)
    if message_type == "get_state":
        room = ROOMS.get(client.get("room_id"))
        if room and websocket in room["players"]:
            payload = sync_payload(room)
            payload["force"] = True
            return await safe_send(websocket, payload)
        return

    if message_type == "create_room":
        if client["room_id"]:
            return await safe_send(websocket, {"type": "error", "msg": "请先退出当前房间"})
        room_id = new_room_id()
        ROOMS[room_id] = {
            "pwd": str(data.get("pwd", "")).strip()[:32],
            "players": {websocket},
            "roles": {"r": None, "b": None},
            "role_sessions": {"r": None, "b": None},
            "disconnect_tasks": {},
            "spectator_sessions": {},
            "spectator_disconnect_tasks": {},
            "round_started": False,
            "snapshot": new_game_snapshot(),
            "version": 0,
            "processed_moves": {},
            "history": [],
            "undo_requester": None,
            "chat_history": [],
            "next_spectator_number": 1,
        }
        client["room_id"] = room_id
        client["spectator_number"] = None
        await safe_send(
            websocket,
            {"type": "room_joined", "room_id": room_id, "count": 1, "roles": {"r": False, "b": False}},
        )
        return await broadcast_lobby_list()

    if message_type == "join_room":
        if client["room_id"]:
            return await safe_send(websocket, {"type": "error", "msg": "请先退出当前房间"})
        room_id = str(data.get("id", ""))
        room = ROOMS.get(room_id)
        if not room:
            return await safe_send(websocket, {"type": "error", "msg": "房间不存在或已解散"})
        if room["pwd"] and room["pwd"] != str(data.get("pwd", "")).strip():
            return await safe_send(websocket, {"type": "error", "msg": "房间密码错误"})

        join_session_id = clean_text(data.get("session_id"), 80)
        spectator = len(room["players"]) >= 2
        room["players"].add(websocket)
        client["room_id"] = room_id
        client["session_id"] = join_session_id or None
        if spectator:
            client["spectator_number"] = room["next_spectator_number"]
            room["next_spectator_number"] += 1
            if join_session_id:
                room["spectator_sessions"][join_session_id] = client["spectator_number"]
        else:
            client["spectator_number"] = None
        await safe_send(
            websocket,
            {
                "type": "spectator_joined" if spectator else "room_joined",
                "room_id": room_id,
                "count": len(room["players"]),
                "roles": roles_status(room),
            },
        )
        await safe_send(websocket, {"type": "chat_history", "messages": room["chat_history"]})
        await broadcast_room_info(room_id)
        await broadcast_lobby_list()
        if spectator and room["round_started"]:
            await safe_send(websocket, {"type": "start", "new_round": False})
            await safe_send(websocket, sync_payload(room))
        return
    if message_type == "reconnect_spectator":
        room_id = str(data.get("room_id", ""))
        session_id = clean_text(data.get("session_id"), 80)
        room = ROOMS.get(room_id)
        spectator_number = room.get("spectator_sessions", {}).get(session_id) if room and session_id else None
        if not room or not spectator_number:
            return await safe_send(websocket, {"type": "error", "msg": "观战席已失效，请返回大厅重新加入"})
        old_task = room["spectator_disconnect_tasks"].pop(session_id, None)
        if old_task:
            old_task.cancel()
        room["players"].add(websocket)
        client.update(room_id=room_id, side=None, spectator_number=spectator_number, session_id=session_id)
        await safe_send(
            websocket,
            {"type": "reconnect_spectator_success", "count": len(room["players"]), "roles": roles_status(room), "started": room["round_started"]},
        )
        await safe_send(websocket, {"type": "chat_history", "messages": room["chat_history"]})
        await broadcast_room_info(room_id)
        if room["round_started"]:
            payload = sync_payload(room)
            payload["force"] = True
            await safe_send(websocket, payload)
        return

    if message_type == "reconnect":
        room_id, side = str(data.get("room_id", "")), data.get("side")
        session_id = str(data.get("session_id", ""))
        room = ROOMS.get(room_id)
        if not room or side not in ("r", "b"):
            return await safe_send(websocket, {"type": "error", "msg": "房间已解散，无法恢复对局"})
        if not session_id or room["role_sessions"].get(side) != session_id:
            return await safe_send(websocket, {"type": "error", "msg": "对局身份已失效，请返回大厅重新加入"})
        old_player = room["roles"][side]
        if old_player and old_player in room["players"] and old_player is not websocket:
            room["players"].discard(old_player)
            CLIENTS.pop(old_player, None)
            asyncio.create_task(old_player.close(code=4001, reason="同一玩家的新连接已接管"))

        cancel_disconnect_task(room, side)
        room["players"].add(websocket)
        room["roles"][side] = websocket
        client.update(room_id=room_id, side=side, spectator_number=None, session_id=session_id)
        started = room["round_started"]
        await safe_send(
            websocket,
            {
                "type": "reconnect_success",
                "count": len(room["players"]),
                "roles": roles_status(room),
                "started": started,
            },
        )
        await safe_send(websocket, {"type": "chat_history", "messages": room["chat_history"]})
        await notify_room(
            room,
            {"type": "opponent_reconnected", "role": chat_role(client), "nickname": client.get("nickname", "棋友")},
            exclude=websocket,
        )
        await broadcast_room_info(room_id)
        await broadcast_lobby_list()
        if started:
            await safe_send(websocket, sync_payload(room))
        return

    if message_type == "leave_room":
        current_nickname = client.get("nickname", "棋友")
        current_openid = client.get("openid")
        await detach_client(websocket, reserve_role=False)
        CLIENTS[websocket] = {
            "room_id": None, "side": None, "nickname": current_nickname, "cheat_mode": False,
            "spectator_number": None, "openid": current_openid, "session_id": None,
            "pending_chat_ids": set(), "login_task": None, "nickname_task": None,
        }
        return await safe_send(websocket, {"type": "left_room"})

    if message_type in ("join_side", "join"):
        room, side = ROOMS.get(client["room_id"]), data.get("side")
        session_id = str(data.get("session_id", ""))
        if not room or side not in ("r", "b"):
            return await safe_send(websocket, {"type": "error", "msg": "选边参数无效"})
        if not session_id:
            return await safe_send(websocket, {"type": "error", "msg": "缺少玩家身份，请重新进入游戏"})
        if room["roles"][side] is not None:
            return await safe_send(websocket, {"type": "error", "msg": "该位置已被占用"})
        old_side = client["side"]
        if old_side in ("r", "b") and room["roles"][old_side] is websocket:
            room["roles"][old_side] = None
            room["role_sessions"][old_side] = None
        room["roles"][side], client["side"] = websocket, side
        client["spectator_number"] = None
        client["session_id"] = session_id
        room["role_sessions"][side] = session_id
        await safe_send(websocket, {"type": "join_success", "side": side})
        await broadcast_room_info(client["room_id"])
        await broadcast_lobby_list()
        if game_started(room) and not room["round_started"]:
            room["round_started"] = True
            room["snapshot"] = new_game_snapshot()
            room["version"] = 0
            room["processed_moves"] = {}
            room["history"] = []
            room["undo_requester"] = None
            await notify_room(room, {"type": "start", "new_round": True})
        return

    if message_type in ("cancel_side", "cancel_join"):
        room, side = ROOMS.get(client["room_id"]), client["side"]
        if room and side in ("r", "b") and room["roles"][side] is websocket:
            room["roles"][side], client["side"] = None, None
            room["role_sessions"][side] = None
            room["round_started"] = False
            cancel_disconnect_task(room, side)
            await safe_send(websocket, {"type": "cancel_success"})
            await broadcast_room_info(client["room_id"])
            await broadcast_lobby_list()
        return

    if message_type in ("move", "action", "sync_board"):
        room, side = ROOMS.get(client["room_id"]), client["side"]
        authorized = room and side in ("r", "b") and room["roles"][side] is websocket
        if not authorized:
            return await safe_send(websocket, {"type": "error", "msg": "观战者或未选边玩家不能操作棋局"})
        if message_type == "move":
            if not room["round_started"] or room["snapshot"].get("gameStatus") == "gameover" or not valid_move(data):
                return await safe_send(websocket, {"type": "error", "msg": "走棋数据无效"})
            move_id = clean_text(data.get("move_id"), 80)
            base_version = data.get("base_version")
            if move_id and move_id in room["processed_moves"]:
                duplicate = sync_payload(room)
                duplicate.update({"force": True, "move_id": move_id, "duplicate": True})
                return await safe_send(websocket, duplicate)
            if isinstance(base_version, int) and base_version != room["version"]:
                rejected = sync_payload(room)
                rejected.update({
                    "force": True,
                    "rejected_move_id": move_id,
                    "msg": "棋盘状态已更新，请按同步后的棋盘继续",
                })
                return await safe_send(websocket, rejected)
            from_pos, to_pos = data["from"], data["to"]
            board = room["snapshot"]["board"]
            moving_piece = board[from_pos["r"]][from_pos["c"]]
            captured_piece = board[to_pos["r"]][to_pos["c"]]
            if not moving_piece:
                return await safe_send(websocket, {"type": "error", "msg": "起点没有棋子"})
            if not client.get("cheat_mode"):
                if room["snapshot"]["turn"] != side:
                    return await safe_send(websocket, {"type": "error", "msg": "尚未轮到你走棋"})
                if not moving_piece.startswith(side + "_"):
                    return await safe_send(websocket, {"type": "error", "msg": "不能移动对方棋子"})
                if captured_piece and captured_piece.startswith(side + "_"):
                    return await safe_send(websocket, {"type": "error", "msg": "不能吃掉己方棋子"})
            room["history"].append(copy.deepcopy(room["snapshot"]))
            if len(room["history"]) > 200:
                room["history"] = room["history"][-200:]
            if captured_piece:
                capture_owner = moving_piece[0] if client.get("cheat_mode") else side
                room["snapshot"]["capturedPieces"][capture_owner].append(captured_piece)
            board[to_pos["r"]][to_pos["c"]] = moving_piece
            board[from_pos["r"]][from_pos["c"]] = None
            room["snapshot"]["lastMove"] = {"from": from_pos, "to": to_pos}
            room["snapshot"]["turn"] = "b" if room["snapshot"]["turn"] == "r" else "r"
            if captured_piece in ("r_j", "b_j"):
                room["snapshot"]["gameStatus"] = "gameover"
                room["snapshot"]["winner"] = moving_piece[0]
                room["snapshot"]["endReason"] = "将死"
            previous_version = room["version"]
            room["version"] += 1
            if move_id:
                room["processed_moves"][move_id] = room["version"]
                if len(room["processed_moves"]) > 100:
                    oldest_ids = list(room["processed_moves"])[:-100]
                    for oldest_id in oldest_ids:
                        room["processed_moves"].pop(oldest_id, None)
            move_payload = {
                "type": "move",
                "from": from_pos,
                "to": to_pos,
                "move_id": move_id,
                "base_version": previous_version,
                "version": room["version"],
                "movingSide": moving_piece[0],
                "cheat": bool(client.get("cheat_mode")),
            }
            await safe_send(websocket, {"type": "move_ack", "move_id": move_id, "version": room["version"]})
            return await notify_room(room, move_payload, exclude=websocket)
        elif message_type == "sync_board":
            if not valid_board(data.get("board")):
                return await safe_send(websocket, {"type": "error", "msg": "棋盘同步数据无效"})
            room["snapshot"] = {
                "board": [row[:] for row in data["board"]],
                "turn": "b" if data.get("turn") == "b" else "r",
                "lastMove": data.get("lastMove"),
                "capturedPieces": data.get("capturedPieces", {"r": [], "b": []}),
                "gameStatus": data.get("game_status", room["snapshot"].get("gameStatus", "playing")),
                "winner": data.get("winner", room["snapshot"].get("winner")),
                "endReason": clean_text(data.get("end_reason", room["snapshot"].get("endReason", "")), 40),
            }
            room["version"] += 1
        elif message_type == "action":
            action = data.get("action")
            if action == "undo_request":
                room["undo_requester"] = side
            elif action == "undo_reject":
                room["undo_requester"] = None
            elif action == "resign":
                room["snapshot"]["gameStatus"] = "gameover"
                room["snapshot"]["winner"] = "b" if side == "r" else "r"
                room["snapshot"]["endReason"] = "对方认输"
                room["version"] += 1
            elif action == "draw_accept":
                room["snapshot"]["gameStatus"] = "gameover"
                room["snapshot"]["winner"] = None
                room["snapshot"]["endReason"] = "双方同意和棋"
                room["version"] += 1
            elif action == "game_over":
                winner = data.get("winner")
                if winner in ("r", "b") and room["snapshot"].get("gameStatus") != "gameover":
                    room["snapshot"]["gameStatus"] = "gameover"
                    room["snapshot"]["winner"] = winner
                    room["snapshot"]["endReason"] = clean_text(data.get("reason"), 40) or "对局结束"
                    room["version"] += 1
                return await notify_room(
                    room,
                    {"type": "game_over", "winner": room["snapshot"].get("winner"), "reason": room["snapshot"].get("endReason", "对局结束")},
                )
            elif action == "undo_accept" and room["history"]:
                restored = room["history"].pop()
                requester = room.get("undo_requester")
                if requester and restored["turn"] != requester and room["history"]:
                    restored = room["history"].pop()
                room["snapshot"] = restored
                room["version"] += 1
                room["undo_requester"] = None
                await notify_room(room, data, exclude=websocket)
                return await notify_room(room, sync_payload(room))
        return await notify_room(room, data, exclude=websocket)

    if message_type == "chat":
        room = ROOMS.get(client["room_id"])
        message = clean_text(data.get("msg"), 30)
        if room and message:
            message_id = clean_text(data.get("message_id"), 80) or secrets.token_hex(8)
            pending_ids = client.setdefault("pending_chat_ids", set())
            if message_id in pending_ids:
                return
            if len(pending_ids) >= 3:
                return await safe_send(
                    websocket,
                    {"type": "content_rejected", "scope": "chat", "msg": "待审核消息较多，请稍候", "message_id": message_id},
                )
            pending_ids.add(message_id)
            await safe_send(websocket, {"type": "chat_pending", "message_id": message_id})
            run_in_background(process_chat(websocket, client, client["room_id"], message, message_id))
        return

    await safe_send(websocket, {"type": "error", "msg": "不支持的消息类型"})


async def handler(websocket, path=None):
    del path
    CLIENTS[websocket] = {
        "room_id": None, "side": None, "nickname": "棋友", "cheat_mode": False,
        "spectator_number": None, "openid": None, "session_id": None,
        "pending_chat_ids": set(), "login_task": None, "nickname_task": None,
    }
    LOGGER.info("客户端连接，当前连接数=%s", len(CLIENTS))
    await safe_send(
        websocket,
        {"type": "server_capabilities", "reliable_moves": True, "async_chat_review": True, "spectator_reconnect": True},
    )
    try:
        async for message in websocket:
            try:
                await handle_message(websocket, message)
            except json.JSONDecodeError:
                await safe_send(websocket, {"type": "error", "msg": "消息不是有效 JSON"})
            except Exception:
                LOGGER.exception("处理客户端消息失败")
                await safe_send(websocket, {"type": "error", "msg": "服务器处理消息失败"})
    except ConnectionClosed:
        pass
    finally:
        await detach_client(websocket, reserve_role=True)
        LOGGER.info("客户端断开，当前连接数=%s", len(CLIENTS))


async def health_check(path, request_headers):
    del request_headers
    if path == "/health":
        body = b"OK"
        return HTTPStatus.OK, [("Content-Type", "text/plain"), ("Content-Length", "2")], body
    if path == "/security-status":
        body = json.dumps(
            {
                "wechat_content_security_enabled": WECHAT_CONTENT_SECURITY_ENABLED,
                "wechat_credentials_configured": bool(WECHAT_APPID and WECHAT_APPSECRET),
                "fail_closed": WECHAT_SECURITY_FAIL_CLOSED,
                "forbidden_words": len(FORBIDDEN_WORDS),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return HTTPStatus.OK, [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))], body
    return None


async def main():
    port = int(os.environ.get("PORT", "10000"))
    async with websockets.serve(
        handler,
        "0.0.0.0",
        port,
        process_request=health_check,
        ping_interval=25,
        ping_timeout=45,
        close_timeout=10,
        max_size=MAX_MESSAGE_SIZE,
        max_queue=32,
    ):
        LOGGER.info("服务器已启动，端口=%s，断线宽限期=%s秒", port, DISCONNECT_GRACE_SECONDS)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
