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
        "type": "room_info", 
        "count": len(room["players"]),
        "watchers": len(room["spectators"]),
        "roles": { "r": room["roles"]["r"] is not None, "b": room["roles"]["b"] is not None }
    }
    for client in list(room["players"]) + list(room["spectators"]):
        try: await client.send(json.dumps(info))
        except: pass

async def send_room_list(websocket):
    # 【修复】：现在会发送所有房间，不再过滤掉满人的房间！加入了 status 状态。
    room_list = [
        {
            "id": rid, 
            "count": len(r["players"]), 
            "watchers": len(r["spectators"]),
            "hasPwd": bool(r["pwd"]),
            "status": r["status"]
        } 
        for rid, r in ROOMS.items()
    ]
    try: await websocket.send(json.dumps({"type": "room_list", "rooms": room_list}))
    except: pass

async def handle_disconnect(websocket):
    if websocket in CLIENTS:
        room_id = CLIENTS[websocket]["room_id"]
        side = CLIENTS[websocket]["side"]
        
        if room_id and room_id in ROOMS:
            room = ROOMS[room_id]
            is_empty = False
            
            # 分离玩家和观战者的离线处理
            if side == "s":
                if websocket in room["spectators"]:
                    room["spectators"].remove(websocket)
            else:
                if websocket in room["players"]:
                    room["players"].remove(websocket)

            if len(room["players"]) == 0 and len(room["spectators"]) == 0:
                del ROOMS[room_id]
                is_empty = True
            elif len(room["players"]) == 0:
                # 玩家全走了只剩观战者，解散房间
                for s in list(room["spectators"]):
                    try: await s.send(json.dumps({"type": "left_room"}))
                    except: pass
                del ROOMS[room_id]
                is_empty = True

            if not is_empty and side != "s":
                for p in list(room["players"]) + list(room["spectators"]):
                    try: await p.send(json.dumps({"type": "opponent_offline"}))
                    except: pass
                await broadcast_room_info(room_id)
        
        CLIENTS[websocket] = {"room_id": None, "side": None}

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
                    ROOMS[room_id] = {
                        "pwd": msg_data.get("pwd", "").strip(), 
                        "players": {websocket}, 
                        "spectators": set(),
                        "roles": {"r": None, "b": None},
                        "status": "waiting"
                    }
                    CLIENTS[websocket] = {"room_id": room_id, "side": None}
                    await websocket.send(json.dumps({"type": "room_joined", "room_id": room_id, "count": 1, "roles": {"r": False, "b": False}}))
                
                elif msg_type == "join_room":
                    room_id = msg_data.get("id")
                    if room_id not in ROOMS:
                        await websocket.send(json.dumps({"type": "error", "msg": "房间不存在或已解散"})); continue
                    room = ROOMS[room_id]
                    if room["pwd"] and room["pwd"] != msg_data.get("pwd", "").strip():
                        await websocket.send(json.dumps({"type": "error", "msg": "房间密码错误"})); continue
                    
                    # 【核心功能】：满人时，作为观战者加入
                    if len(room["players"]) >= 2:
                        room["spectators"].add(websocket)
                        CLIENTS[websocket] = {"room_id": room_id, "side": "s"}
                        await websocket.send(json.dumps({"type": "room_joined_spectator", "room_id": room_id}))
                        # 通知房间内的真实玩家上传最新棋盘快照，同步给观战者
                        for p in list(room["players"]):
                            try: await p.send(json.dumps({"type": "request_sync"}))
                            except: pass
                    else:
                        room["players"].add(websocket)
                        CLIENTS[websocket] = {"room_id": room_id, "side": None}
                        roles_status = {"r": room["roles"]["r"] is not None, "b": room["roles"]["b"] is not None}
                        await websocket.send(json.dumps({"type": "room_joined", "room_id": room_id, "count": len(room["players"]), "roles": roles_status}))
                    await broadcast_room_info(room_id)

                elif msg_type == "reconnect":
                    room_id = msg_data.get("room_id")
                    side = msg_data.get("side")
                    if room_id in ROOMS:
                        room = ROOMS[room_id]
                        if side == "s":
                            room["spectators"].add(websocket)
                        else:
                            room["players"].add(websocket)
                            room["roles"][side] = websocket
                        CLIENTS[websocket] = {"room_id": room_id, "side": side}
                        for p in list(room["players"]) + list(room["spectators"]):
                            if p != websocket:
                                try: await p.send(json.dumps({"type": "opponent_reconnected"}))
                                except: pass

                elif msg_type == "leave_room":
                    room_id = CLIENTS[websocket]["room_id"]
                    side = CLIENTS[websocket]["side"]
                    if room_id in ROOMS:
                        room = ROOMS[room_id]
                        if side == "s":
                            if websocket in room["spectators"]:
                                room["spectators"].remove(websocket)
                        else:
                            for p in list(room["players"]) + list(room["spectators"]):
                                if p != websocket:
                                    try: await p.send(json.dumps({"type": "opponent_left"}))
                                    except: pass
                            del ROOMS[room_id]
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
                            room["status"] = "playing"
                            for p in list(room["players"]) + list(room["spectators"]):
                                try: await p.send(json.dumps({"type": "start"}))
                                except: pass

                elif msg_type in ["cancel_side", "cancel_join"]:
                    room_id = CLIENTS[websocket]["room_id"]
                    side = CLIENTS[websocket]["side"]
                    if room_id and side and room_id in ROOMS:
                        ROOMS[room_id]["roles"][side] = None
                        ROOMS[room_id]["status"] = "waiting"
                        CLIENTS[websocket]["side"] = None
                        await websocket.send(json.dumps({"type": "cancel_success"}))
                        await broadcast_room_info(room_id)

                # 【同步协议】：将真实玩家的棋盘画面接力发给观战者
                elif msg_type == "sync_state":
                    room_id = CLIENTS[websocket]["room_id"]
                    if room_id in ROOMS:
                        for s in list(ROOMS[room_id]["spectators"]):
                            try: await s.send(json.dumps(msg_data))
                            except: pass

                elif msg_type in ["move", "action", "chat"]:
                    room_id = CLIENTS[websocket]["room_id"]
                    if room_id in ROOMS:
                        # 走棋和聊天广播给所有玩家+观战者
                        for p in list(ROOMS[room_id]["players"]) + list(ROOMS[room_id]["spectators"]):
                            if p != websocket:
                                try: await p.send(json.dumps(msg_data))
                                except: pass
            except Exception as e:
                print(f"解析出错: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await handle_disconnect(websocket)

async def main():
    port = int(os.environ.get("PORT", 10000))
    async with websockets.serve(handler, "0.0.0.0", port, ping_interval=None, ping_timeout=None):
        print(f"服务器已启动，正在监听端口 {port}...")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
