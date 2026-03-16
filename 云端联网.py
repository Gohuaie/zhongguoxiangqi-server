import os
import asyncio
import websockets
import json

connected_clients = set()
roles = {'r': None, 'b': None}

async def broadcast_room_info():
    info = {
        "type": "room_info",
        "count": len(connected_clients),
        "roles": {
            "r": roles['r'] is not None,
            "b": roles['b'] is not None
        }
    }
    msg = json.dumps(info)
    for client in connected_clients:
        try:
            await client.send(msg)
        except:
            pass

async def handler(websocket):
    global roles, connected_clients
    print("新连接建立...")
    connected_clients.add(websocket)
    my_side = None

    await broadcast_room_info()

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            # 1. 加入阵营
            if msg_type == "join":
                side = data.get("side")
                if roles.get(side) is None:
                    roles[side] = websocket
                    my_side = side
                    await websocket.send(json.dumps({"type": "join_success", "side": side}))
                    print(f"玩家加入了 {side} 方")
                    
                    await broadcast_room_info()
                    
                    if roles['r'] and roles['b']:
                        await roles['r'].send(json.dumps({"type": "start"}))
                        await roles['b'].send(json.dumps({"type": "start"}))
                else:
                    await websocket.send(json.dumps({"type": "error", "message": "手慢了，该阵营刚被对方抢走啦！"}))

            # 2. 取消准备 (新功能)
            elif msg_type == "cancel_join":
                if my_side and roles.get(my_side) == websocket:
                    roles[my_side] = None
                    print(f"玩家取消了 {my_side} 方的准备")
                    my_side = None
                    await websocket.send(json.dumps({"type": "cancel_success"}))
                    await broadcast_room_info() # 通知所有人位置空出来了

            # 3. 游戏指令转发
            else:
                if my_side:
                    opponent_side = 'b' if my_side == 'r' else 'r'
                    if roles.get(opponent_side):
                        await roles[opponent_side].send(message)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.remove(websocket)
        if my_side and roles.get(my_side) == websocket:
            roles[my_side] = None
            print(f"{my_side} 方断开了连接")
            opponent_side = 'b' if my_side == 'r' else 'r'
            if roles.get(opponent_side):
                try:
                    await roles[opponent_side].send(json.dumps({"type": "opponent_left", "message": "对手已断开连接"}))
                except:
                    pass
        await broadcast_room_info()

async def main():
    port = int(os.environ.get("PORT", 8765))
    print(f"服务器运行在 0.0.0.0:{port}")
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())
