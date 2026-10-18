
import asyncio
import json
import re
import contextlib
from contextlib import asynccontextmanager
from typing import Literal, Optional,Any
from urllib.parse import unquote
import os
from pathlib import Path



from dotenv import dotenv_values
import httpx
import hashlib

import pandas as pd

from server import mcp, mcp_app
from fastapi import FastAPI,Query,Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

import yaml


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # load ledger data once at startup
    rows = await asyncio.to_thread(fetch_export)
    app.state.df = build_df(rows)
    app.state.vocab = build_vocab(app.state.df)
    app.state.prompt = build_prompt(app.state.vocab)

    #  existing MCP session manager running
    async with mcp.session_manager.run():
        yield

app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins = "*",
    allow_credentials=True,
    allow_methods =["*"],
    allow_headers=["*"]
)
DEFAULTS : dict[str,Any] ={
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",

}


def coerce(key:str, value:Any)->Any:
    if value is None:
        return None
    
    if key in {"port","workers"}:
        return int(value)
    
    if key =="debug":
        return str(value).strip().lower() in {"true","1","yes","on"}
    
    return str(value)

# print(coerce("port", "8000"))      # 8000
# print(coerce("workers", "4"))      # 4
# print(coerce("debug", "YES"))      # True
# print(coerce("debug", "false"))     # False
# print(coerce("log_level", "debug")) # "debug"
# print(coerce("api_key", 12345))  # "12345"
# print(coerce("port", None)) 

def normalize_key(key: str) ->str:
    if key.upper() =="NUM_WORKERS":
        return "workers"
    return key.lower()


def load_yaml_config(env:str)-> dict[str,Any]:
    path = Path(f'config.{env}.yaml')
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}

    return {normalize_key(k): v for k,v in data.items()}

def load_dotenv_config()->dict[str,Any]:
    values = dotenv_values("env")
    return {normalize_key(k): v for k,v in values.items() if v is not None}

def load_os_env_config() ->dict[str,Any]:
    return{
        normalize_key(k.removeprefix("APP_")):v
        for k,v in os.environ.items()
        if k.startswith("APP_")

    }


def parse_set_overrides(set_params: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}

    for item in set_params:
        raw = item

        # Support both ?set=log_level=error and ?set=set=log_level=error
        if raw.startswith("set="):
            raw = raw.removeprefix("set=")

        key, separator, value = raw.partition("=")

        if not separator:
            continue
        
        key = normalize_key(key.strip())
        overrides[key] = coerce(key, value.strip())

    return overrides


@app.get("/q1/config")
def get_config(
    env:str=Query(default="development"),
    set:list[str] = Query(default =[]),
):
    merged : dict[str,Any]=dict(DEFAULTS)
    #lowest to highest precedence
    #defaults ->YAML -> .en ->OSenvironemnt -> query overrides
    layers = [
        load_yaml_config(env),
        load_dotenv_config(),
        load_os_env_config(),
        parse_set_overrides(set)
    ]

    for layer in layers:
        for key,value in layer.items():
            merged[key] = coerce(key,value)

    merged["api_key"] = "****"

    return merged




@app.get("/debug/query")
def debug_query(set: list[str] = Query(default=[])):
    return {
        "raw_set": set,
        "parsed": parse_set_overrides(set),
    }

# BASE ="https://exam.sanand.workers.dev/questionData?"
# def fetch_all_orders(max_pages :int=200,timeout :int=60) -> list[dict]:
#     params={
#         "email": os.environ["EMAIL"],
#         "quizSign":os.environ["QUIZ_SIGN"],
#         "questionId": "q-ledger-agent-server",
#         "path":"/orders",
#         "page":1
#     }
#     orders : list[dict]=[]
#     url: str |None = BASE

#     with httpx.Client(timeout=timeout) as client:
#         for _ in range(max_pages):
#             r = client.get(url,params=params)
#             r.raise_for_status()
#             data = r.json()
#             orders.extend(data["orders"])

#             url = data.get("next")
#             if not url:
#                 break
#             params = None

#         else:
#             raise RuntimeError("Hit max_pages without reaching the last page")
#     # print(len({o["id"] for o in orders})) 
#     return orders

# def fetch_export() -> list[dict]:
#     params={
#         "email": os.environ["EMAIL"],
#         "quizSign":os.environ["QUIZ_SIGN"],
#         "questionId": "q-ledger-agent-server",
#         "path":"/export"
#     }
#     export_rows : list[dict]=[]
 

#     with httpx.Client(timeout=60) as client:
    
#         r = client.get(BASE,params=params)
#         r.raise_for_status()
    
#         export_rows =[json.loads(line) for line in r.text.splitlines() if line.strip()]

#     # print(len({o["id"] for o in export_rows})) 
#     return export_rows

# order = fetch_all_orders()
# export = fetch_export()

# print({o["id"] for o in order} == {o["id"] for o in export})



BASE = "https://exam.sanand.workers.dev/questionData"
CACHE = "export_raw.ndjson"
RATES = {"USD": 1.0, "EUR": 1.07, "INR": 0.01172}
MODEL = os.environ.get("MODEL", "gpt-4o-mini")
DATE_FIELD = "created_at"  # business-date column; switch to "updated_at" if needed


client = AsyncOpenAI(timeout=9, max_retries=1)  # reads OPENAI_API_KEY / OPENAI_BASE_URL

# ---------- data loading ----------
def fetch_export(timeout: int = 60) -> list[dict]:
    params = {
        "email": os.environ["EMAIL"],
        "quizSign": unquote(os.environ["QUIZ_SIGN"]),
        "questionId": "q-ledger-agent-server",
        "path": "/export",
    }
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(BASE, params=params)
            r.raise_for_status()
        text = r.text
        with open(CACHE, "w") as f:
            f.write(text)
    except Exception:
        if not os.path.exists(CACHE):
            raise
        text = open(CACHE).read()  # fall back to cached copy
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def build_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in ("created_at", "updated_at"):
        df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601")
    df = df.sort_values("updated_at").drop_duplicates("id", keep="last").copy()
    df["status"] = df["status"].str.strip().str.lower()
    df["amount_usd"] = df["amount"] * df["currency"].map(RATES)
    ist = df[DATE_FIELD].dt.tz_convert("Asia/Kolkata")
    df["date_ist"] = ist.dt.tz_localize(None).dt.normalize()
    df["month"] = df["date_ist"].dt.strftime("%Y-%m")
    return df


def build_vocab(df: pd.DataFrame) -> dict:
    return {
        "regions": sorted(df["region"].unique().tolist()),
        "products": sorted(df["product"].unique().tolist()),
        "currencies": sorted(df["currency"].unique().tolist()),
        "min_date": str(df["date_ist"].min().date()),
        "max_date": str(df["date_ist"].max().date()),
    }


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     rows = await asyncio.to_thread(fetch_export)
#     app.state.df = build_df(rows)
#     app.state.vocab = build_vocab(app.state.df)
#     app.state.prompt = build_prompt(app.state.vocab)
#     yield


# app = FastAPI(lifespan=lifespan)
# app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------- spec ----------
Metric = Literal["revenue_usd", "order_count", "units_sold", "customer_count", "avg_order_usd"]
GroupBy = Literal["product", "region", "customer", "currency", "month"]


class Spec(BaseModel):
    metric: Metric
    statuses: list[Literal["paid", "refunded", "void"]] = ["paid"]
    region: Optional[list[str]] = None
    product: Optional[list[str]] = None
    customer: Optional[list[str]] = None
    currency: Optional[list[str]] = None
    start: Optional[str] = None  # YYYY-MM-DD inclusive, IST
    end: Optional[str] = None    # YYYY-MM-DD exclusive, IST
    group_by: Optional[GroupBy] = None
    order: Literal["desc", "asc"] = "desc"
    limit: Optional[int] = None
    answer_shape: Literal["number", "name", "list"] = "number"


def build_prompt(v: dict) -> str:
    return f"""You are the Ledger Agent for Acme Appliances. You NEVER calculate or answer directly.
You convert a plain-English question into ONE JSON query spec that code will execute. Output ONLY JSON.

DATA VOCABULARY (use these exact spellings):
- regions: {v['regions']}
- products: {v['products']}
- currencies: {v['currencies']}
- customer ids look like "C1145"
- statuses: "paid", "refunded", "void"
- data covers orders from {v['min_date']} to {v['max_date']} (IST dates)

BUSINESS DEFINITIONS:
- Revenue / sales / income / turnover = metric "revenue_usd" with statuses ["paid"].
- Refund questions use statuses ["refunded"]. "How many refunds / refunded orders" = metric "order_count".
  "Refund amount / value / total refunded" = metric "revenue_usd" with statuses ["refunded"].
- Void/cancelled orders use statuses ["void"].
- "Orders" = metric "order_count". "Units / items / quantity sold" = "units_sold".
  "How many (distinct/unique) customers" = "customer_count". "Average order value" = "avg_order_usd".
- All money is USD. If a question says "all orders regardless of status", use all three statuses.
- Dates are IST. start is INCLUSIVE, end is EXCLUSIVE, format YYYY-MM-DD.
  March 2026 -> start 2026-03-01, end 2026-04-01. Q1 2026 -> 2026-01-01 to 2026-04-01.
  A single day D -> start D, end D+1. "Between A and B" (inclusive of B) -> end = B+1.
- "Now" is {v['max_date']} (the latest date in the data) unless the question states a date.
  "This month" = month containing now. "Last month" = the calendar month before it.
  "Last N days" = end is now+1 day, start is end minus N days.
- If no time period is mentioned, set start and end to null.
- Filters (region, product, customer, currency) are lists of exact values, or null.
- "Which / top / best / most / highest" -> group_by + order "desc" + limit 1 + answer_shape "name"
  (use "number" if they ask for the value itself). "Lowest / least / worst" -> order "asc".
  "Top N" -> limit N, answer_shape "list". Months as group_by use "month".
- answer_shape "number" for a single value.

SPEC FORMAT:
{{"metric": "revenue_usd|order_count|units_sold|customer_count|avg_order_usd",
 "statuses": ["paid"], "region": null, "product": null, "customer": null, "currency": null,
 "start": null, "end": null, "group_by": null, "order": "desc", "limit": null,
 "answer_shape": "number"}}

EXAMPLES:
Q: What was the total revenue in USD from the North region in March 2026?
{{"metric":"revenue_usd","statuses":["paid"],"region":["North"],"start":"2026-03-01","end":"2026-04-01","answer_shape":"number"}}
Q: Which product earned the most revenue overall?
{{"metric":"revenue_usd","statuses":["paid"],"group_by":"product","order":"desc","limit":1,"answer_shape":"name"}}
Q: How many orders were refunded for the Blender?
{{"metric":"order_count","statuses":["refunded"],"product":["Blender"],"answer_shape":"number"}}
Q: How many distinct customers bought a Kettle in the South in February 2026?
{{"metric":"customer_count","statuses":["paid"],"region":["South"],"product":["Kettle"],"start":"2026-02-01","end":"2026-03-01","answer_shape":"number"}}
Q: Top 3 customers by spend?
{{"metric":"revenue_usd","statuses":["paid"],"group_by":"customer","order":"desc","limit":3,"answer_shape":"list"}}
"""


# ---------- normalize ----------
def _match(value: str, allowed: list[str]) -> str:
    v = value.strip().lower()
    lookup = {a.lower(): a for a in allowed}
    if v in lookup:
        return lookup[v]
    for cand in (v.rstrip("s"), v.replace(" ", "")):
        for k, a in lookup.items():
            if cand == k or cand == k.replace(" ", ""):
                return a
    for k, a in lookup.items():  # substring either way, e.g. "the blender"
        if k in v or v in k:
            return a
    raise ValueError(f"Unknown value {value!r}; allowed: {allowed}")


def normalize(spec: Spec, df: pd.DataFrame, vocab: dict) -> Spec:
    if spec.region:
        spec.region = [_match(x, vocab["regions"]) for x in spec.region]
    if spec.product:
        spec.product = [_match(x, vocab["products"]) for x in spec.product]
    if spec.currency:
        spec.currency = [_match(x, vocab["currencies"]) for x in spec.currency]
    if spec.customer:
        spec.customer = [x.strip().upper() for x in spec.customer]
    for f in ("start", "end"):
        val = getattr(spec, f)
        if val:
            pd.Timestamp(val)  # raises if invalid
    return spec


# ---------- execute ----------
def _agg(d: pd.DataFrame, metric: str, group_col: Optional[str]):
    if group_col:
        g = d.groupby(group_col)
        if metric == "revenue_usd":
            return g["amount_usd"].sum()
        if metric == "order_count":
            return g["id"].nunique()
        if metric == "units_sold":
            return g["qty"].sum()
        if metric == "customer_count":
            return g["customer"].nunique()
        return g["amount_usd"].mean()
    if metric == "revenue_usd":
        return d["amount_usd"].sum()
    if metric == "order_count":
        return d["id"].nunique()
    if metric == "units_sold":
        return d["qty"].sum()
    if metric == "customer_count":
        return d["customer"].nunique()
    return d["amount_usd"].mean() if len(d) else 0.0


def _clean(x, metric: str):
    if metric in ("revenue_usd", "avg_order_usd"):
        return round(float(x), 2)
    return int(x)


def execute(spec: Spec, df: pd.DataFrame):
    d = df[df["status"].isin(spec.statuses)]
    for col in ("region", "product", "customer", "currency"):
        vals = getattr(spec, col)
        if vals:
            d = d[d[col].isin(vals)]
    if spec.start:
        d = d[d["date_ist"] >= pd.Timestamp(spec.start)]
    if spec.end:
        d = d[d["date_ist"] < pd.Timestamp(spec.end)]

    if not spec.group_by:
        return _clean(_agg(d, spec.metric, None), spec.metric)

    s = _agg(d, spec.metric, spec.group_by)
    if len(s) == 0:
        return [] if spec.answer_shape == "list" else None
    s = s.reset_index()
    s.columns = ["key", "val"]
    s = s.sort_values(["val", "key"], ascending=[spec.order == "asc", True])
    if spec.limit:
        s = s.head(spec.limit)
    if spec.answer_shape == "list":
        return s["key"].tolist()
    top = s.iloc[0]
    if spec.answer_shape == "name":
        return top["key"]
    return _clean(top["val"], spec.metric)


# ---------- LLM ----------
def _extract_json(text: str) -> str:
    text = re.sub(r"```(?:json)?", "", text)
    i, j = text.find("{"), text.rfind("}")
    return text[i : j + 1] if i != -1 and j != -1 else text


async def _chat(messages: list[dict]) -> str:
    try:
        r = await client.chat.completions.create(
            model=MODEL, messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        if "response_format" in str(e).lower() or "json" in str(e).lower():
            r = await client.chat.completions.create(model=MODEL, messages=messages)
        else:
            raise
    return r.choices[0].message.content or ""


async def question_to_spec(question: str, app_: FastAPI) -> Spec:
    messages = [
        {"role": "system", "content": app_.state.prompt},
        {"role": "user", "content": question},
    ]
    last_err = None
    for _ in range(2):
        text = await _chat(messages)
        try:
            spec = Spec.model_validate_json(_extract_json(text))
            return normalize(spec, app_.state.df, app_.state.vocab)
        except (ValidationError, ValueError) as e:
            last_err = e
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"Invalid spec: {e}. Return corrected JSON only."},
            ]
    raise last_err


# ---------- endpoint ----------
class Ask(BaseModel):
    question: str


async def _answer(question: str):
    spec = await question_to_spec(question, app)
    return execute(spec, app.state.df)


@app.post("/")
@app.post("/ask")
async def ask(body: Ask):
    try:
        answer = await asyncio.wait_for(_answer(body.question), timeout=13)
    except Exception as e:
        print("ERROR:", repr(e))
        answer = None
    return {"answer": answer}

   
app.mount("/", mcp_app)

