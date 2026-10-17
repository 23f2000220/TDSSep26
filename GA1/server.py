import hashlib

from mcp.server.mcpserver import MCPServer, Context


mcp= MCPServer("challenge-server")
mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True,host="0.0.0.0")

EXAM_EMAIL = "23f2000220@ds.study.iitm.ac.in"
normalizedEmail = EXAM_EMAIL.strip().lower()

@mcp.tool()
async def solve_challenge(ctx: Context)->str:
    res = (ctx.headers or {}).get("x-exam-challenge")
    if res is None:
        return "missing"
    output =(f"{res}:{normalizedEmail}").encode("utf-8")
    hashed = hashlib.sha256(output)
    ans = hashed.hexdigest()[:16]
    return ans

# if __name__=="__main__":
#     mcp.run(transport="streamable-http",host="0.0.0.0"port=8000)