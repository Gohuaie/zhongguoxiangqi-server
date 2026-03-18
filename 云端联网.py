import asyncio
import websockets
import json
import random
import string
import os

ROOMS = {}
CLIENTS = {}

async def broadcast_room_info(room_id):
    if room_id not in ROOMS: return
    room = ROOMS[room_id]
    info = {
        "type": "room_info", "count": len(room["players"]),
        "roles": { "r": room["roles"]["r"] is not None, "b": room["roles"]["b"] is not None }
    }
    for player in list(room["players"]):
        try: await player.send(json.dumps(info))
        except: pass

async def send_room_list(websocket):
    room_list = [{"id": rid, "count": len(r["players"]), "hasPwd": bool(r["pwd"]), "isPlaying": (r["roles"]["r"] is not None and r["roles"]["b"] is not None)} for rid, r in ROOMS.items()]
    try: await websocket.send(json.dumps({"type": "room_list", "rooms": room_list}))
    except: pass

async def handle_disconnect(websocket):
    """处理网络异常导致的被动断线"""
    if websocket in CLIENTS:
        room_id = CLIENTS[websocket]["room_id"]
        side = CLIENTS[websocket]["side"]
        
        if room_id and room_id in ROOMS:
            room = ROOMS[room_id]
            if websocket in room["players"]: 
                room["players"].remove(websocket)
            
            if len(room["players"]) == 0:
                # 连观战者都没了，直接销毁房间
                del ROOMS[room_id]
            else:
                # 【核心修复】：如果是网络异常掉线，绝不清空座位！保留位置！
                # 只向对手和观众发送“网络波动”的黄色提醒
                if side and room["roles"][side] == websocket:
                    for p in list(room["players"]):
                        try: await p.send(json.dumps({"type": "opponent_offline"}))
                        except: pass
                
                await broadcast_room_info(room_id)
        
        if websocket in CLIENTS:
            del CLIENTS[websocket]

async def handler(websocket, path):
    CLIENTS[websocket] = {"room_id": None, "side": None}
    try:
        async for message in websocket:
            try:
                msg_data = json.loads(message)
                if "data" in msg_data and isinstance(msg_data["data"], str):
                    try: msg_data = json.loads(msg_data["data"])
                    except: pass
                
                msg_type = msg_data.get("type")

                if msg_type == "ping": continue
                elif msg_type == "get_rooms": await send_room_list(websocket)
                
                elif msg_type == "create_room":
                    room_id = ''.join(random.choices(string.digits, k=4))
                    while room_id in ROOMS: room_id = ''.join(random.choices(string.digits, k=4))
                    ROOMS[room_id] = {"pwd": msg_data.get("pwd", "").strip(), "players": {websocket}, "roles": {"r": None, "b": None}}
                    CLIENTS[websocket]["room_id"] = room_id
                    await websocket.send(json.dumps({"type": "room_joined", "room_id": room_id, "count": 1, "roles": {"r": False, "b": False}}))
                
                elif msg_type == "join_room":
                    room_id = msg_data.get("id")
                    if room_id not in ROOMS:
                        await websocket.send(json.dumps({"type": "error", "msg": "房间不存在或已解散"})); continue
                    room = ROOMS[room_id]
                    if room["pwd"] and room["pwd"] != msg_data.get("pwd", "").strip():
                        await websocket.send(json.dumps({"type": "error", "msg": "房间密码错误"})); continue
                    
                    is_spectator = len(room["players"]) >= 2
                    room["players"].add(websocket)
                    CLIENTS[websocket]["room_id"] = room_id
                    roles_status = {"r": room["roles"]["r"] is not None, "b": room["roles"]["b"] is not None}
                    
                    if is_spectator:
                        await websocket.send(json.dumps({"type": "spectator_joined", "room_id": room_id, "count": len(room["players"]), "roles": roles_status}))
                    else:
                        await websocket.send(json.dumps({"type": "room_joined", "room_id": room_id, "count": len(room["players"]), "roles": roles_status}))
                    
                    await broadcast_room_info(room_id)

                    if is_spectator and roles_status["r"] and roles_status["b"]:
                        await websocket.send(json.dumps({"type": "start"}))
                        red_player = room["roles"]["r"]
                        if red_player:
                            try: await red_player.send(json.dumps({"type": "request_sync"}))
                            except: pass

                elif msg_type == "reconnect":
                    room_id = msg_data.get("room_id")
                    side = msg_data.get("side")
                    if room_id in ROOMS:
                        room = ROOMS[room_id]
                        room["players"].add(websocket)
                        if side:
                            room["roles"][side] = websocket
                            CLIENTS[websocket]["side"] = side
                        CLIENTS[websocket]["room_id"] = room_id
                        
                        for p in list(room["players"]):
                            if p != websocket:
                                try: await p.send(json.dumps({"type": "opponent_reconnected"}))
                                except: pass
                        
                        if room["roles"]["r"] is not None and room["roles"]["b"] is not None:
                            await websocket.send(json.dumps({"type": "start"}))
                            red_player = room["roles"]["r"]
                            try: await red_player.send(json.dumps({"type": "request_sync"}))
                            except: pass
                        
                        await broadcast_room_info(room_id)

                elif msg_type == "leave_room":
                    # 【核心修复】：主动点击“退出房间”，才彻底清空他的专属座位！
                    room_id = CLIENTS[websocket]["room_id"]
                    side = CLIENTS[websocket]["side"]
                    if room_id in ROOMS:
                        room = ROOMS[room_id]
                        if side and room["roles"][side] == websocket:
                            room["roles"][side] = None # 没收座位
                        if websocket in room["players"]:
                            room["players"].remove(websocket)
                        
                        if len(room["players"]) == 0:
                            del ROOMS[room_id]
                        else:
                            if side:
                                for p in list(room["players"]):
                                    try: await p.send(json.dumps({"type": "opponent_left"}))
                                    except: pass
                            await broadcast_room_info(room_id)
                            
                    CLIENTS[websocket] = {"room_id": None, "side": None}
                    await websocket.send(json.dumps({"type": "left_room"}))

                elif msg_type in ["join_side", "join"]:
                    room_id = CLIENTS[websocket]["room_id"]
                    if not room_id or room_id not in ROOMS: continue
                    side = msg_data.get("side")
                    room = ROOMS[room_id]
                    if room["roles"][side] is None:
                        old_side = CLIENTS[websocket]["side"]
                        if old_side: room["roles"][old_side] = None
                        room["roles"][side] = websocket
                        CLIENTS[websocket]["side"] = side
                        await websocket.send(json.dumps({"type": "join_success", "side": side}))
                        await broadcast_room_info(room_id)
                        if room["roles"]["r"] is not None and room["roles"]["b"] is not None:
                            for p in list(room["players"]):
                                try: await p.send(json.dumps({"type": "start"}))
                                except: pass

                elif msg_type in ["cancel_side", "cancel_join"]:
                    room_id = CLIENTS[websocket]["room_id"]
                    side = CLIENTS[websocket]["side"]
                    if room_id and side and room_id in ROOMS:
                        ROOMS[room_id]["roles"][side] = None
                        CLIENTS[websocket]["side"] = None
                        await websocket.send(json.dumps({"type": "cancel_success"}))
                        await broadcast_room_info(room_id)

                elif msg_type in ["move", "action", "chat", "sync_board"]:
                    room_id = CLIENTS[websocket]["room_id"]
                    if room_id in ROOMS:
                        for p in list(ROOMS[room_id]["players"]):
                            if p != websocket:
                                try: await p.send(json.dumps(msg_data))
                                except: pass
            except Exception as e:
                print(f"解析出错: {e}")
    except websockets.exceptions.ConnectionClosed: pass
    finally: await handle_disconnect(websocket)

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handler, "0.0.0.0", port, ping_interval=None, ping_timeout=None):
        print(f"服务器已启动，正在监听端口 {port}...")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
