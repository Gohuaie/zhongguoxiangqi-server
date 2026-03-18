import asyncio
import websockets
import json
import random
import string
import os

ROOMS = {}
CLIENTS = {}

async def broadcast_room_info(room_id):
    if room_id not in ROOMS:
        return
    room = ROOMS[room_id]
    info = {
        "type": "room_info",
        "count": len(room["players"]),
        "roles": {
            "r": room["roles"]["r"] is not None,
            "b": room["roles"]["b"] is not None
        }
    }
    message = json.dumps(info)
    for player in list(room["players"]):
        try:
            await player.send(message)
        except:
            pass

async def send_room_list(websocket):
    room_list = [
        {
            "id": rid, 
            "count": len(r["players"]), 
            "hasPwd": bool(r["pwd"])
        } 
        for rid, r in ROOMS.items() if len(r["players"]) < 2
    ]
    try:
        await websocket.send(json.dumps({"type": "room_list", "rooms": room_list}))
    except:
        pass

async def handle_disconnect(websocket):
    if websocket in CLIENTS:
        room_id = CLIENTS[websocket]["room_id"]
        side = CLIENTS[websocket]["side"]
        
        if room_id and room_id in ROOMS:
            room = ROOMS[room_id]
            if websocket in room["players"]:
                room["players"].remove(websocket)
            
            if side and room["roles"][side] == websocket:
                room["roles"][side] = None
                
            if len(room["players"]) == 0:
                del ROOMS[room_id]
            else:
                for p in list(room["players"]):
                    try:
                        await p.send(json.dumps({"type": "opponent_left"}))
                    except:
                        pass
                await broadcast_room_info(room_id)
        
        CLIENTS[websocket] = {"room_id": None, "side": None}

async def handler(websocket, path):
    CLIENTS[websocket] = {"room_id": None, "side": None}
    try:
        async for message in websocket:
            try:
                msg_data = json.loads(message)
                if "data" in msg_data and isinstance(msg_data["data"], str):
                    try:
                        msg_data = json.loads(msg_data["data"])
                    except:
                        pass
                
                msg_type = msg_data.get("type")

                if msg_type == "ping":
                    continue
                elif msg_type == "get_rooms":
                    await send_room_list(websocket)
                elif msg_type == "create_room":
                    room_id = ''.join(random.choices(string.digits, k=4))
                    while room_id in ROOMS:
                        room_id = ''.join(random.choices(string.digits, k=4))
                    pwd = msg_data.get("pwd", "").strip()
                    ROOMS[room_id] = {"pwd": pwd, "players": {websocket}, "roles": {"r": None, "b": None}}
                    CLIENTS[websocket]["room_id"] = room_id
                    await websocket.send(json.dumps({"type": "room_joined", "room_id": room_id, "count": 1, "roles": {"r": False, "b": False}}))
                elif msg_type == "join_room":
                    room_id = msg_data.get("id")
                    pwd = msg_data.get("pwd", "").strip()
                    if room_id not in ROOMS:
                        await websocket.send(json.dumps({"type": "error", "msg": "房间不存在或已解散"}))
                        continue
                    room = ROOMS[room_id]
                    if len(room["players"]) >= 2:
                        await websocket.send(json.dumps({"type": "error", "msg": "该房间已经满员了"}))
                        continue
                    if room["pwd"] and room["pwd"] != pwd:
                        await websocket.send(json.dumps({"type": "error", "msg": "房间密码错误"}))
                        continue
                    room["players"].add(websocket)
                    CLIENTS[websocket]["room_id"] = room_id
                    roles_status = {"r": room["roles"]["r"] is not None, "b": room["roles"]["b"] is not None}
                    await websocket.send(json.dumps({"type": "room_joined", "room_id": room_id, "count": len(room["players"]), "roles": roles_status}))
                    await broadcast_room_info(room_id)
                elif msg_type == "leave_room":
                    await handle_disconnect(websocket)
                    await websocket.send(json.dumps({"type": "left_room"}))
                elif msg_type == "join_side" or msg_type == "join":
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
                elif msg_type == "cancel_side" or msg_type == "cancel_join":
                    room_id = CLIENTS[websocket]["room_id"]
                    side = CLIENTS[websocket]["side"]
                    if room_id and side and room_id in ROOMS:
                        ROOMS[room_id]["roles"][side] = None
                        CLIENTS[websocket]["side"] = None
                        await websocket.send(json.dumps({"type": "cancel_success"}))
                        await broadcast_room_info(room_id)
                elif msg_type in ["move", "action", "chat"]:
                    room_id = CLIENTS[websocket]["room_id"]
                    if room_id in ROOMS:
                        for p in list(ROOMS[room_id]["players"]):
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
    # 【核心终极修复】：彻底关闭服务器的主动查岗，防止错杀手机端！
    async with websockets.serve(handler, "0.0.0.0", port, ping_interval=None, ping_timeout=None):
        print(f"服务器已启动，正在监听端口 {port}...")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
