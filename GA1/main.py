import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


from fastapi import FastAPI,Query,Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()


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




