import asyncio
import copy
import json
import logging
import os
import secrets
import string
from http import HTTPStatus

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
MAX_MESSAGE_SIZE = 64 * 1024

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
    }


def clean_text(value, limit):
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()[:limit]


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


async def safe_send(websocket, payload):
    try:
        await websocket.send(json.dumps(payload, ensure_ascii=False))
        return True
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

    if message_type == "set_nickname":
        client["nickname"] = clean_text(data.get("nickname"), 10) or "棋友"
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
            "round_started": False,
            "snapshot": new_game_snapshot(),
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

        spectator = len(room["players"]) >= 2
        room["players"].add(websocket)
        client["room_id"] = room_id
        if spectator:
            client["spectator_number"] = room["next_spectator_number"]
            room["next_spectator_number"] += 1
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
        client.update(room_id=room_id, side=side, spectator_number=None)
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
        await detach_client(websocket, reserve_role=False)
        CLIENTS[websocket] = {"room_id": None, "side": None, "nickname": current_nickname, "cheat_mode": False, "spectator_number": None}
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
        room["role_sessions"][side] = session_id
        await safe_send(websocket, {"type": "join_success", "side": side})
        await broadcast_room_info(client["room_id"])
        await broadcast_lobby_list()
        if game_started(room) and not room["round_started"]:
            room["round_started"] = True
            room["snapshot"] = new_game_snapshot()
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
            if not room["round_started"] or not valid_move(data):
                return await safe_send(websocket, {"type": "error", "msg": "走棋数据无效"})
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
            move_payload = {
                "type": "move",
                "from": from_pos,
                "to": to_pos,
                "cheat": bool(client.get("cheat_mode")),
                "movingSide": moving_piece[0],
            }
            await notify_room(room, move_payload, exclude=websocket)
            authoritative = sync_payload(room)
            authoritative["force"] = True
            return await notify_room(room, authoritative)
        elif message_type == "sync_board":
            if not valid_board(data.get("board")):
                return await safe_send(websocket, {"type": "error", "msg": "棋盘同步数据无效"})
            room["snapshot"] = {
                "board": [row[:] for row in data["board"]],
                "turn": "b" if data.get("turn") == "b" else "r",
                "lastMove": data.get("lastMove"),
                "capturedPieces": data.get("capturedPieces", {"r": [], "b": []}),
            }
        elif message_type == "action":
            action = data.get("action")
            if action == "undo_request":
                room["undo_requester"] = side
            elif action == "undo_reject":
                room["undo_requester"] = None
            elif action == "undo_accept" and room["history"]:
                restored = room["history"].pop()
                requester = room.get("undo_requester")
                if requester and restored["turn"] != requester and room["history"]:
                    restored = room["history"].pop()
                room["snapshot"] = restored
                room["undo_requester"] = None
                await notify_room(room, data, exclude=websocket)
                return await notify_room(room, sync_payload(room))
        return await notify_room(room, data, exclude=websocket)

    if message_type == "chat":
        room = ROOMS.get(client["room_id"])
        message = clean_text(data.get("msg"), 30)
        if room and message:
            payload = {
                "type": "chat",
                "msg": message,
                "nickname": client.get("nickname", "棋友"),
                "role": chat_role(client),
            }
            remember_chat(room, payload)
            return await notify_room(room, payload)
        return

    await safe_send(websocket, {"type": "error", "msg": "不支持的消息类型"})


async def handler(websocket, path=None):
    del path
    CLIENTS[websocket] = {"room_id": None, "side": None, "nickname": "棋友", "cheat_mode": False, "spectator_number": None}
    LOGGER.info("客户端连接，当前连接数=%s", len(CLIENTS))
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
    return None


async def main():
    port = int(os.environ.get("PORT", "10000"))
    async with websockets.serve(
        handler,
        "0.0.0.0",
        port,
        process_request=health_check,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=MAX_MESSAGE_SIZE,
        max_queue=32,
    ):
        LOGGER.info("服务器已启动，端口=%s，断线宽限期=%s秒", port, DISCONNECT_GRACE_SECONDS)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
