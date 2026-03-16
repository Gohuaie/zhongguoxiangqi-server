import os
import asyncio
import websockets
import json

# 记录所有连接到该网页的玩家
connected_clients = set()
# 记录已选定的阵营
roles = {'r': None, 'b': None}

async def broadcast_room_info():
    """向大厅里所有人广播当前房间人数和阵营选择情况"""
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

    # 有人进来，广播刷新房间人数
    await broadcast_room_info()

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            # 1. 处理玩家点击"确定"加入阵营
            if msg_type == "join":
                side = data.get("side")
                if roles.get(side) is None:
                    roles[side] = websocket
                    my_side = side
                    await websocket.send(json.dumps({"type": "join_success", "side": side}))
                    print(f"玩家确认加入了 {side} 方")
                    
                    # 广播更新，让别人看到这个阵营被选了
                    await broadcast_room_info()
                    
                    # 双方都选好，通知开战
                    if roles['r'] and roles['b']:
                        await roles['r'].send(json.dumps({"type": "start"}))
                        await roles['b'].send(json.dumps({"type": "start"}))
                else:
                    await websocket.send(json.dumps({"type": "error", "message": "手慢了，该阵营刚被对方抢走啦！"}))

            # 2. 转发游戏内的指令
            else:
                if my_side:
                    opponent_side = 'b' if my_side == 'r' else 'r'
                    if roles.get(opponent_side):
                        await roles[opponent_side].send(message)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # 玩家断线清理
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
        # 广播有人离开
        await broadcast_room_info()

async def main():
    port = int(os.environ.get("PORT", 8765))
    print(f"服务器运行在 0.0.0.0:{port}")
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())
