import asyncio
import websockets
import json
import random
import string
import os

# 全局状态管理
# ROOMS 结构: { "room_id": {"pwd": "xxx", "players": set(), "roles": {"r": websocket, "b": websocket}} }
ROOMS = {}
# CLIENTS 结构: websocket -> {"room_id": "123", "side": "r"}
CLIENTS = {}

async def broadcast_room_info(room_id):
    """向房间内的所有玩家广播当前房间的人数和选角状态"""
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
    for player in room["players"]:
        try:
            await player.send(message)
        except:
            pass

async def send_room_list(websocket):
    """向请求大厅的玩家发送当前可用房间列表"""
    # 只返回未满员的房间
    room_list = [
        {
            "id": rid, 
            "count": len(r["players"]), 
            "hasPwd": bool(r["pwd"])
        } 
        for rid, r in ROOMS.items() if len(r["players"]) < 2
    ]
    await websocket.send(json.dumps({"type": "room_list", "rooms": room_list}))

async def handle_disconnect(websocket):
    """处理玩家掉线或离开房间"""
    if websocket in CLIENTS:
        room_id = CLIENTS[websocket]["room_id"]
        side = CLIENTS[websocket]["side"]
        
        if room_id and room_id in ROOMS:
            room = ROOMS[room_id]
            # 从房间中移除玩家
            if websocket in room["players"]:
                room["players"].remove(websocket)
            
            # 释放座位
            if side and room["roles"][side] == websocket:
                room["roles"][side] = None
                
            # 如果房间空了，直接销毁房间
            if len(room["players"]) == 0:
                del ROOMS[room_id]
            else:
                # 如果还有人，通知对方“对手已离开”
                for p in room["players"]:
                    try:
                        await p.send(json.dumps({"type": "opponent_left"}))
                    except:
                        pass
                await broadcast_room_info(room_id)
        
        # 清理该连接的全局记录
        CLIENTS[websocket] = {"room_id": None, "side": None}

async def handler(websocket, path):
    CLIENTS[websocket] = {"room_id": None, "side": None}
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_data = json.loads(data.get("data", "{}")) # 微信小程序发送格式解析
            except:
                msg_data = json.loads(message) # 兼容原生测试格式

            msg_type = msg_data.get("type")

            # 1. 获取房间列表
            if msg_type == "get_rooms":
                await send_room_list(websocket)

            # 2. 创建房间
            elif msg_type == "create_room":
                # 生成4位随机房间号
                room_id = ''.join(random.choices(string.digits, k=4))
                while room_id in ROOMS:
                    room_id = ''.join(random.choices(string.digits, k=4))
                
                pwd = msg_data.get("pwd", "").strip()
                ROOMS[room_id] = {
                    "pwd": pwd,
                    "players": {websocket},
                    "roles": {"r": None, "b": None}
                }
                CLIENTS[websocket]["room_id"] = room_id
                
                await websocket.send(json.dumps({
                    "type": "room_joined", 
                    "room_id": room_id, 
                    "count": 1, 
                    "roles": {"r": False, "b": False}
                }))

            # 3. 加入房间
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
                
                # 验证通过，加入房间
                room["players"].add(websocket)
                CLIENTS[websocket]["room_id"] = room_id
                
                roles_status = {"r": room["roles"]["r"] is not None, "b": room["roles"]["b"] is not None}
                await websocket.send(json.dumps({
                    "type": "room_joined", 
                    "room_id": room_id, 
                    "count": len(room["players"]), 
                    "roles": roles_status
                }))
                await broadcast_room_info(room_id)

            # 4. 离开房间 (退回大厅)
            elif msg_type == "leave_room":
                await handle_disconnect(websocket)
                await websocket.send(json.dumps({"type": "left_room"}))

            # 5. 房间内：选择阵营
            elif msg_type == "join_side" or msg_type == "join":
                room_id = CLIENTS[websocket]["room_id"]
                if not room_id or room_id not in ROOMS:
                    continue
                    
                side = msg_data.get("side")
                room = ROOMS[room_id]
                
                # 如果这个座位没人占
                if room["roles"][side] is None:
                    # 先清空自己可能占用的旧座位
                    old_side = CLIENTS[websocket]["side"]
                    if old_side:
                        room["roles"][old_side] = None
                        
                    # 坐上新座位
                    room["roles"][side] = websocket
                    CLIENTS[websocket]["side"] = side
                    
                    await websocket.send(json.dumps({"type": "join_success", "side": side}))
                    await broadcast_room_info(room_id)
                    
                    # 检查是否双发都准备完毕，开始游戏
                    if room["roles"]["r"] is not None and room["roles"]["b"] is not None:
                        for p in room["players"]:
                            await p.send(json.dumps({"type": "start"}))

            # 6. 房间内：取消选择阵营
            elif msg_type == "cancel_side" or msg_type == "cancel_join":
                room_id = CLIENTS[websocket]["room_id"]
                side = CLIENTS[websocket]["side"]
                if room_id and side and room_id in ROOMS:
                    ROOMS[room_id]["roles"][side] = None
                    CLIENTS[websocket]["side"] = None
                    await websocket.send(json.dumps({"type": "cancel_success"}))
                    await broadcast_room_info(room_id)

            # 7. 游戏对战指令中转 (move, action, chat)
            elif msg_type in ["move", "action", "chat"]:
                room_id = CLIENTS[websocket]["room_id"]
                if room_id in ROOMS:
                    for p in ROOMS[room_id]["players"]:
                        if p != websocket: # 只发给房间里的另一个人
                            await p.send(json.dumps(msg_data))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await handle_disconnect(websocket)

async def main():
    # Render 云端环境通常会动态分配端口，通过环境变量 PORT 获取
    port = int(os.environ.get("PORT", 10000))
    # 绑定到 0.0.0.0 以允许外部访问
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"服务器已启动，正在监听端口 {port}...")
        await asyncio.Future()  # 永久运行

if __name__ == "__main__":
    asyncio.run(main())
