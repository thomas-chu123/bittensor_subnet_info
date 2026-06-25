import asyncio
import bittensor as bt
from bittensor_cli.src.bittensor.balances import Balance
import numpy as np
from typing import List, Dict, Optional, AsyncGenerator
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import logging
from logging.handlers import RotatingFileHandler
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG
import copy
import json
import os
import redis
import pickle
import signal
import subprocess
import threading
import time

# 設置日誌（必須在最前面，在任何其他代碼之前）
LOG_FILE = "server.log"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging() -> None:
    """輸出到 console 與 server.log，讓 PyCharm/直接執行都能保留紀錄。"""
    formatter = logging.Formatter(LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(getattr(handler, "_subnet_console_handler", False) for handler in root_logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler._subnet_console_handler = True
        root_logger.addHandler(console_handler)

    if not any(getattr(handler, "_subnet_file_handler", False) for handler in root_logger.handlers):
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler._subnet_file_handler = True
        root_logger.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.setLevel(logging.INFO)
        uvicorn_logger.propagate = True


def build_uvicorn_log_config() -> Dict:
    """讓 python main.py 啟動時，uvicorn 自己的 log 也寫入 server.log。"""
    log_config = copy.deepcopy(UVICORN_LOGGING_CONFIG)
    log_config["handlers"]["server_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "default",
        "filename": LOG_FILE,
        "maxBytes": 10 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
    }
    log_config["handlers"]["server_access_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "access",
        "filename": LOG_FILE,
        "maxBytes": 10 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
    }

    for logger_name, handler_name in (
        ("uvicorn", "server_file"),
        ("uvicorn.error", "server_file"),
        ("uvicorn.access", "server_access_file"),
    ):
        handlers = log_config["loggers"][logger_name].setdefault("handlers", [])
        if handler_name not in handlers:
            handlers.append(handler_name)

    return log_config


configure_logging()
logger = logging.getLogger(__name__)

# ==================== 配置 ====================
NETWORK = 'finney'
SUBTENSOR_NETWORK = 'finney'
MAX_CONCURRENT_REQUESTS = 10  # 同時請求數量限制

# Redis 快取配置
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_DB = 0
CACHE_TTL_BASIC = 86400  # 基本信息快取時間：24小時（不變化的數據）
CACHE_TTL_METAGRAPH = 3600  # metagraph 快取時間：60分鐘（動態數據）
CACHE_TTL_SNAPSHOT = 3600  # 整頁 subnet 列表快取時間：60分鐘
SUBNETS_SNAPSHOT_CACHE_KEY = f"subnets_snapshot:{NETWORK}"
SUBNET_METRICS_CACHE_PREFIX = "subnet_metrics"
SUBNET_METRICS_REQUIRED_FIELDS = (
    'invest_value',
    'registration_cost',
    'n_miners',
    'active_incentive_miners',
    'total_stake',
    'total_emissions',
    'incentive_mean',
)

# 初始化 Redis 連接
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=False)
    redis_client.ping()
    logger.info("✓ Redis 連接成功")
    REDIS_ENABLED = True
except Exception as e:
    logger.warning(f"⚠️ Redis 連接失敗: {e}，將禁用快取功能")
    redis_client = None
    REDIS_ENABLED = False

# ==================== FastAPI 應用 ====================
app = FastAPI(title="Bittensor Subnet Info", description="Subnet 分析儀表盤")


def _terminate_child_processes() -> int:
    """Best-effort termination for subprocesses directly owned by this server."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        logger.warning(f"無法查詢子程序: {e}")
        return 0

    child_pids = [int(pid) for pid in result.stdout.split() if pid.isdigit()]
    for pid in child_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as e:
            logger.warning(f"無法終止子程序 {pid}: {e}")

    return len(child_pids)


def _stop_website_process(delay: float = 0.3) -> None:
    """Stop this website process after the API response has had time to flush."""
    time.sleep(delay)
    child_count = _terminate_child_processes()
    logger.info(f"收到網站停止請求，已嘗試停止 {child_count} 個子程序，準備停止主程序")

    try:
        if redis_client is not None:
            redis_client.close()
    except Exception as e:
        logger.warning(f"關閉 Redis 連線時發生錯誤: {e}")

    os.kill(os.getpid(), signal.SIGTERM)

# ==================== 快取輔助函數 ====================

def get_from_cache(key: str) -> Optional[Dict]:
    """從 Redis 獲取快取"""
    if not REDIS_ENABLED:
        return None
    try:
        cached = redis_client.get(key)
        if cached:
            return pickle.loads(cached)
    except Exception as e:
        logger.warning(f"讀取快取失敗 ({key}): {e}")
    return None


def set_to_cache(key: str, value: Dict, ttl: int = 300) -> bool:
    """設置 Redis 快取"""
    if not REDIS_ENABLED:
        return False
    try:
        redis_client.setex(key, ttl, pickle.dumps(value))
        return True
    except Exception as e:
        logger.warning(f"寫入快取失敗 ({key}): {e}")
        return False


def clear_cache(pattern: str = "*") -> int:
    """清除指定模式的快取"""
    if not REDIS_ENABLED:
        return 0
    try:
        keys = redis_client.keys(pattern)
        if keys:
            return redis_client.delete(*keys)
    except Exception as e:
        logger.warning(f"清除快取失敗: {e}")
    return 0

# ==================== 輔助函數 ====================

def calculate_gini_coefficient(values: np.ndarray) -> float:
    """
    計算Gini系數，用來衡量incentive分佈的均勻性
    0 = 完全均勻分佈
    1 = 完全不均勻分佈（一人獲得所有）
    """
    if len(values) == 0 or np.sum(values) == 0:
        return 0.0
    
    sorted_values = np.sort(values)
    n = len(values)
    cumsum = np.cumsum(sorted_values)
    
    gini = (2 * np.sum(np.arange(1, n + 1) * sorted_values)) / (n * np.sum(sorted_values)) - (n + 1) / n
    return float(max(0, min(1, gini)))


def calculate_invest_value_details(incentives: np.ndarray) -> Dict:
    """
    計算投資價值 (0~1) 與可追蹤的中間數據
    基於incentive分佈的均勻性與實際獲得incentive的miner覆蓋率
    0.0 = 分佈極度不均勻（風險高）
    1.0 = 分佈均勻且多數miner都有incentive（風險低）
    """
    total_count = int(len(incentives))
    if len(incentives) == 0 or np.sum(incentives) == 0:
        return {
            'invest_value': 0.0,
            'active_ratio': 0.0,
            'gini': 0.0,
            'distribution_score': 0.0,
            'active_count': 0,
            'total_count': total_count,
        }
    
    active_count = int(np.count_nonzero(incentives > 0))
    active_ratio = float(active_count / len(incentives))
    gini = calculate_gini_coefficient(incentives)
    distribution_score = 1.0 - gini
    invest_value = (distribution_score * 0.7) + (active_ratio * 0.3)
    return {
        'invest_value': float(invest_value),
        'active_ratio': active_ratio,
        'gini': gini,
        'distribution_score': distribution_score,
        'active_count': active_count,
        'total_count': total_count,
    }


def calculate_invest_value(incentives: np.ndarray) -> float:
    """保留單純回傳分數的介面，供其他呼叫端使用。"""
    return float(calculate_invest_value_details(incentives)['invest_value'])


def build_metagraph_metrics(metagraph_data: Dict) -> Dict:
    """從metagraph數據計算前端顯示所需的統計值。"""
    if 'validator_permit' not in metagraph_data:
        raise ValueError("metagraph 快取缺少 validator_permit，需重新抓取")

    netuid = metagraph_data.get('netuid')
    incentives = np.array(metagraph_data['incentives'])
    has_incentives = len(incentives) > 0
    active_incentive_miners = int(np.count_nonzero(incentives > 0)) if has_incentives else 0
    total_uids = int(metagraph_data.get('n', len(metagraph_data.get('hotkeys', []))))
    validator_permits = np.array(metagraph_data.get('validator_permit', []), dtype=bool)
    validator_count = int(np.count_nonzero(validator_permits)) if len(validator_permits) > 0 else 0
    miner_count = max(total_uids - validator_count, 0)
    invest_details = calculate_invest_value_details(incentives)
    incentive_mean = float(np.mean(incentives)) if has_incentives else None
    incentive_std = float(np.std(incentives)) if has_incentives else None
    logger.info(
        "投資價值計算 netuid=%s invest_value=%.6f formula=(0.7*distribution_score)+(0.3*active_ratio) "
        "distribution_score=%.6f gini=%.6f active_ratio=%.6f active_incentive_miners=%s "
        "incentive_count=%s total_uids=%s miners=%s validators=%s incentive_mean=%s incentive_std=%s",
        netuid,
        invest_details['invest_value'],
        invest_details['distribution_score'],
        invest_details['gini'],
        invest_details['active_ratio'],
        active_incentive_miners,
        invest_details['total_count'],
        total_uids,
        miner_count,
        validator_count,
        f"{incentive_mean:.8f}" if incentive_mean is not None else "N/A",
        f"{incentive_std:.8f}" if incentive_std is not None else "N/A",
    )

    return {
        'n_total_uids': total_uids,
        'n_miners': miner_count,
        'n_validators': validator_count,
        'invest_value': invest_details['invest_value'],
        'active_incentive_miners': active_incentive_miners,
        'incentive_active_ratio': invest_details['active_ratio'],
        'total_stake': float(np.sum(metagraph_data['stakes'])) / 1e9,
        'total_emissions': float(np.sum(metagraph_data['emissions'])) / 1e9,
        'incentive_mean': incentive_mean,
        'incentive_std': incentive_std,
    }


def get_cached_metagraph_metrics(netuid: int) -> Optional[Dict]:
    """只讀取Redis metagraph快取，不觸發新的鏈上查詢。"""
    cached_data = get_from_cache(f"metagraph:{netuid}")
    if cached_data is None:
        return None

    try:
        return build_metagraph_metrics(cached_data)
    except Exception as e:
        logger.warning(f"Subnet {netuid} metagraph 快取統計計算失敗: {e}")
        return None


def get_subnet_metrics_cache_key(netuid: int) -> str:
    return f"{SUBNET_METRICS_CACHE_PREFIX}:{netuid}"


def get_cached_subnet_metrics(netuid: int) -> Optional[Dict]:
    """讀取已計算完成、可直接供 UI 使用的 subnet 指標快取。"""
    cached_data = get_from_cache(get_subnet_metrics_cache_key(netuid))
    if not isinstance(cached_data, dict):
        return None

    missing_fields = [
        field for field in SUBNET_METRICS_REQUIRED_FIELDS
        if field not in cached_data
    ]
    if missing_fields:
        logger.info(f"Subnet {netuid} UI 指標快取缺少欄位 {missing_fields}，忽略舊快取")
        return None

    return cached_data


def set_cached_subnet_metrics(subnet: Dict) -> None:
    """保存 UI 顯示所需的 per-subnet 指標，避免每次只靠 raw metagraph 重算。"""
    if subnet.get('is_partial'):
        return

    netuid = subnet.get('netuid')
    if netuid is None:
        return

    missing_fields = [
        field for field in SUBNET_METRICS_REQUIRED_FIELDS
        if field not in subnet
    ]
    if missing_fields:
        logger.warning(f"Subnet {netuid} UI 指標快取寫入略過，缺少欄位: {missing_fields}")
        return

    cached_record = {
        'netuid': int(netuid),
        'name': subnet.get('name', 'N/A'),
        'owner': subnet.get('owner', 'N/A'),
        'is_partial': False,
        'created_at': time.time(),
    }
    for field in SUBNET_METRICS_REQUIRED_FIELDS:
        cached_record[field] = subnet.get(field)

    for optional_field in (
        'n_total_uids',
        'n_validators',
        'incentive_active_ratio',
        'incentive_std',
    ):
        if optional_field in subnet:
            cached_record[optional_field] = subnet.get(optional_field)

    set_to_cache(
        get_subnet_metrics_cache_key(int(netuid)),
        cached_record,
        ttl=CACHE_TTL_METAGRAPH
    )


def get_cached_subnets_snapshot() -> Optional[List[Dict]]:
    """讀取整頁 subnet 快取，讓瀏覽器 refresh 不必先重建每個 subnet。"""
    cached_data = get_from_cache(SUBNETS_SNAPSHOT_CACHE_KEY)
    if not isinstance(cached_data, dict):
        return None

    data = cached_data.get('data')
    if not isinstance(data, list):
        return None

    return data


def set_cached_subnets_snapshot(subnets: List[Dict]) -> None:
    """保存整頁 subnet 結果，供下一次 browser refresh 優先使用。"""
    complete_subnets = [subnet for subnet in subnets if not subnet.get('is_partial')]
    if not complete_subnets:
        return

    for subnet in complete_subnets:
        set_cached_subnet_metrics(subnet)

    snapshot = {
        'created_at': time.time(),
        'total_subnets': len(complete_subnets),
        'data': sorted(complete_subnets, key=lambda subnet: subnet['netuid'])
    }
    set_to_cache(SUBNETS_SNAPSHOT_CACHE_KEY, snapshot, ttl=CACHE_TTL_SNAPSHOT)


async def get_subtensor_async() -> bt.Subtensor:
    """連接到Subtensor網絡（異步）"""
    try:
        return bt.Subtensor(network=SUBTENSOR_NETWORK)
    except Exception as e:
        logger.error(f"無法連接到Subtensor: {e}")
        raise HTTPException(status_code=500, detail=f"無法連接到Subtensor: {e}")


async def get_metagraph_data_async(subtensor: bt.Subtensor, netuid: int) -> Optional[Dict]:
    """異步獲取subnet的metagraph數據（帶快取）"""
    cache_key = f"metagraph:{netuid}"
    
    # 嘗試從快取獲取
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        if 'validator_permit' not in cached_data:
            logger.info(f"Subnet {netuid} metagraph 快取缺少 validator_permit，重新抓取")
        else:
            logger.info(f"✓ Subnet {netuid} metagraph 從快取讀取")
            return cached_data
    
    try:
        logger.info(f"開始獲取Subnet {netuid}的metagraph...")
        # bt.Metagraph is synchronous/blocking. Run it in a worker thread so
        # asyncio.gather/as_completed can fetch multiple subnets concurrently.
        metagraph = await asyncio.to_thread(
            bt.Metagraph,
            netuid=netuid,
            lite=True,
            network=NETWORK
        )
        logger.info(f"成功獲取Subnet {netuid}的metagraph，有{len(metagraph.hotkeys)}個hotkeys")


        # 準備數據
        data = {
            'netuid': netuid,
            'n': len(metagraph.hotkeys),
            'hotkeys': metagraph.hotkeys,
            'validator_permit': metagraph.validator_permit.cpu().numpy() if hasattr(metagraph.validator_permit, 'cpu') else np.array(metagraph.validator_permit),
            'incentives': metagraph.incentive.cpu().numpy() if hasattr(metagraph.incentive, 'cpu') else np.array(metagraph.incentive),
            'stakes': metagraph.stake.cpu().numpy() if hasattr(metagraph.stake, 'cpu') else np.array(metagraph.stake),
            'emissions': metagraph.emission.cpu().numpy() if hasattr(metagraph.emission, 'cpu') else np.array(metagraph.emission),
        }
        
        # 寫入快取（5分鐘）
        set_to_cache(cache_key, data, ttl=CACHE_TTL_METAGRAPH)
        
        return data
    except Exception as e:
        logger.error(f"無法獲取Subnet {netuid}的metagraph: {type(e).__name__}: {e}", exc_info=True)
        return None


async def get_subnet_registration_cost_async(subtensor: bt.Subtensor, netuid: int) -> float:
    """異步獲取subnet的registration cost（帶快取）"""
    cache_key = f"reg_cost:{netuid}"
    
    # 嘗試從快取獲取
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        return cached_data.get('cost', 0.0)
    
    try:
        # 嘗試方式1: 使用get_hyperparameter
        try:
            burn_cost_rao = subtensor.get_hyperparameter(
                param_name="Burn", 
                netuid=netuid
            )
            if burn_cost_rao is not None:
                burn_cost = Balance.from_rao(int(burn_cost_rao))
                cost = float(burn_cost.tao)
                # 寫入快取（24小時）
                set_to_cache(cache_key, {'cost': cost}, ttl=CACHE_TTL_BASIC)
                return cost
        except:
            pass
        
        # 嘗試方式2: 使用get_burn_rate
        try:
            burn_rate = subtensor.get_burn_rate(netuid=netuid)
            if burn_rate is not None:
                cost = float(burn_rate)
                # 寫入快取（24小時）
                set_to_cache(cache_key, {'cost': cost}, ttl=CACHE_TTL_BASIC)
                return cost
        except:
            pass
        
        return 0.0
        
    except Exception as e:
        logger.warning(f"無法獲取Subnet {netuid}的registration cost: {e}")
        return 0.0


async def get_subnet_info_async(subtensor: bt.Subtensor, netuid: int) -> Dict:
    """異步獲取subnet的基本信息（帶快取）"""
    cache_key = f"subnet_info:{netuid}"
    
    # 嘗試從快取獲取
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        return cached_data
    
    try:
        subnet_info = subtensor.get_subnet_info(netuid)
        if subnet_info is None:
            return {}
        
        data = {
            'name': getattr(subnet_info, 'name', 'N/A'),
            'owner': getattr(subnet_info, 'owner', 'N/A'),
        }
        
        # 寫入快取（24小時）
        set_to_cache(cache_key, data, ttl=CACHE_TTL_BASIC)
        
        return data
    except Exception as e:
        logger.warning(f"無法獲取Subnet {netuid}的信息: {e}")
        return {}


async def process_single_subnet_fast(subtensor: bt.Subtensor, netuid: int) -> Optional[Dict]:
    """快速獲取subnet的基本數據（不包含metagraph）"""
    try:
        cached_subnet_metrics = get_cached_subnet_metrics(netuid)
        if cached_subnet_metrics is not None:
            return cached_subnet_metrics

        cached_metrics = get_cached_metagraph_metrics(netuid)

        # 快速獲取基本信息
        reg_cost, subnet_info = await asyncio.gather(
            get_subnet_registration_cost_async(subtensor, netuid),
            get_subnet_info_async(subtensor, netuid),
            return_exceptions=True
        )

        metagraph_defaults = {
            'n_total_uids': 0,
            'n_miners': 0,
            'n_validators': 0,
            'total_stake': 0.0,
            'total_emissions': 0.0,
            'invest_value': 0.5,
            'active_incentive_miners': 0,
            'incentive_active_ratio': 0.0,
            'incentive_mean': None,
            'incentive_std': None,
        }
        metagraph_metrics = cached_metrics or metagraph_defaults

        return {
            'netuid': netuid,
            'name': subnet_info.get('name', 'N/A') if isinstance(subnet_info, dict) else 'N/A',
            'registration_cost': float(reg_cost) if reg_cost else 0.0,
            'owner': subnet_info.get('owner', 'N/A') if isinstance(subnet_info, dict) else 'N/A',
            **metagraph_metrics,
            'is_partial': cached_metrics is None
        }
        
    except Exception as e:
        logger.error(f"快速獲取Subnet {netuid}基本數據失敗: {e}")
        return None


async def process_single_subnet_metagraph(subtensor: bt.Subtensor, netuid: int) -> Optional[Dict]:
    """獲取subnet的metagraph數據（計算密集）"""
    try:
        metagraph_data = await get_metagraph_data_async(subtensor, netuid)
        
        if metagraph_data is None:
            return None
        
        metrics = build_metagraph_metrics(metagraph_data)
        
        return {
            'netuid': netuid,
            **metrics,
            'is_partial': False
        }
        
    except Exception as e:
        logger.error(f"獲取Subnet {netuid}的metagraph失敗: {e}")
        return None


async def process_single_subnet(subtensor: bt.Subtensor, netuid: int) -> Optional[Dict]:
    """處理單個subnet的所有數據（並行）"""
    try:
        cached_subnet_metrics = get_cached_subnet_metrics(netuid)
        if cached_subnet_metrics is not None:
            return cached_subnet_metrics

        # 並行獲取所有信息
        metagraph_data, reg_cost, subnet_info = await asyncio.gather(
            get_metagraph_data_async(subtensor, netuid),
            get_subnet_registration_cost_async(subtensor, netuid),
            get_subnet_info_async(subtensor, netuid),
            return_exceptions=True
        )
        
        if metagraph_data is None:
            return None
        
        metrics = build_metagraph_metrics(metagraph_data)
        subnet_info_data = subnet_info if isinstance(subnet_info, dict) else {}
        
        subnet_data = {
            'netuid': netuid,
            'name': subnet_info_data.get('name', 'N/A'),
            'registration_cost': float(reg_cost) if reg_cost else 0.0,
            **metrics,
            'owner': subnet_info_data.get('owner', 'N/A'),
        }
        set_cached_subnet_metrics(subnet_data)

        return subnet_data
        
    except Exception as e:
        logger.error(f"處理Subnet {netuid}時出錯: {e}")
        return None


async def get_all_subnets_data_stream(subtensor: bt.Subtensor, min_invest_value: float = 0.0, max_invest_value: float = 1.0) -> AsyncGenerator[Dict, None]:
    """流式獲取所有subnet的數據，一次一個"""
    try:
        # 獲取所有subnet列表
        all_subnets = subtensor.get_all_subnets_info()
        
        if isinstance(all_subnets, list):
            subnet_netuids = [subnet.netuid for subnet in all_subnets] if all_subnets else []
        else:
            subnet_netuids = list(all_subnets.keys()) if all_subnets else []
        
        logger.info(f"找到 {len(subnet_netuids)} 個subnet")
        
        # 使用信號量限制並行請求數量
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
        async def process_with_semaphore(netuid):
            async with semaphore:
                return await process_single_subnet(subtensor, netuid)
        
        # 使用 as_completed 逐個返回完成的結果
        tasks = {asyncio.create_task(process_with_semaphore(netuid)): netuid for netuid in subnet_netuids}
        
        for task in asyncio.as_completed(tasks):
            try:
                result = await task
                if result is not None:
                    # 檢查是否符合過濾條件
                    if min_invest_value <= result['invest_value'] <= max_invest_value:
                        yield result
            except Exception as e:
                logger.warning(f"處理subnet時出錯: {e}")
                continue
        
    except Exception as e:
        logger.error(f"無法獲取subnet列表: {e}")


async def get_all_subnets_data_async(subtensor: bt.Subtensor) -> List[Dict]:
    """並行獲取所有subnet的數據"""
    try:
        # 獲取所有subnet列表
        all_subnets = subtensor.get_all_subnets_info()
        
        if isinstance(all_subnets, list):
            subnet_netuids = [subnet.netuid for subnet in all_subnets] if all_subnets else []
        else:
            subnet_netuids = list(all_subnets.keys()) if all_subnets else []
        
        logger.info(f"找到 {len(subnet_netuids)} 個subnet")
        
        # 使用信號量限制並行請求數量
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
        async def process_with_semaphore(netuid):
            async with semaphore:
                return await process_single_subnet(subtensor, netuid)
        
        # 並行處理所有subnet
        tasks = [process_with_semaphore(netuid) for netuid in subnet_netuids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 過濾掉失敗的結果
        subnet_data_list = [r for r in results if r is not None and not isinstance(r, Exception)]
        
        return subnet_data_list
        
    except Exception as e:
        logger.error(f"無法獲取subnet列表: {e}")
        return []


def format_tao(value: float) -> str:
    """將值格式化為TAO"""
    if value >= 1000:
        return f"{value / 1000:.4f}k τ"
    elif value >= 1:
        return f"{value:.4f} τ"
    else:
        return f"{value * 1000:.4f}m τ"


# ==================== API 路由 ====================

@app.get("/", response_class=HTMLResponse)
async def get_index():
    """返回主頁面"""
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/subnets")
async def get_subnets(min_invest_value: float = 0.0, max_invest_value: float = 1.0):
    """獲取所有subnet數據（支持過濾）"""
    try:
        cached_snapshot = get_cached_subnets_snapshot()
        if cached_snapshot is not None:
            subnet_data_list = cached_snapshot
        else:
            subtensor = await get_subtensor_async()

            # 並行獲取所有subnet數據
            subnet_data_list = await get_all_subnets_data_async(subtensor)
            set_cached_subnets_snapshot(subnet_data_list)
        
        # 過濾投資價值
        filtered_subnets = [
            s for s in subnet_data_list
            if min_invest_value <= s['invest_value'] <= max_invest_value
        ]
        
        # 按投資價值排序
        sorted_subnets = sorted(filtered_subnets, key=lambda x: x['invest_value'], reverse=True)
        
        return JSONResponse({
            'total_subnets': len(subnet_data_list),
            'filtered_subnets': len(filtered_subnets),
            'data': sorted_subnets
        })
        
    except Exception as e:
        logger.error(f"API錯誤: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def generate_subnets_stream(min_invest_value: float, max_invest_value: float):
    """生成流式subnet數據：分兩階段，先基本信息，再metagraph數據"""
    try:
        cached_snapshot = get_cached_subnets_snapshot()
        if cached_snapshot is not None:
            logger.info(f"✓ 從整頁快取讀取 {len(cached_snapshot)} 個subnets")
            yield json.dumps({
                "type": "total",
                "count": len(cached_snapshot),
                "source": "redis_snapshot"
            }) + "\n"

            for subnet in cached_snapshot:
                yield json.dumps({
                    "type": "subnet",
                    "data": subnet,
                    "source": "redis_snapshot"
                }) + "\n"

            yield json.dumps({"type": "basic_complete", "source": "redis_snapshot"}) + "\n"
            yield json.dumps({
                "type": "complete",
                "basic": len(cached_snapshot),
                "updated": 0,
                "source": "redis_snapshot"
            }) + "\n"
            return

        subtensor = await get_subtensor_async()
        
        # 立即發送總subnet數
        all_subnets = subtensor.get_all_subnets_info()
        if isinstance(all_subnets, list):
            subnet_netuids = [subnet.netuid for subnet in all_subnets] if all_subnets else []
        else:
            subnet_netuids = list(all_subnets.keys()) if all_subnets else []
        
        logger.info(f"找到{len(subnet_netuids)}個subnets，開始兩階段流式傳輸")
        yield json.dumps({"type": "total", "count": len(subnet_netuids)}) + "\n"
        
        # ========== 第一階段：快速發送基本信息 ==========
        logger.info("第一階段：快速發送所有subnet的基本信息...")
        
        # 快速獲取所有基本信息
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
        async def fast_with_semaphore(netuid):
            async with semaphore:
                return await process_single_subnet_fast(subtensor, netuid)
        
        fast_tasks = [fast_with_semaphore(netuid) for netuid in subnet_netuids]
        fast_results = await asyncio.gather(*fast_tasks, return_exceptions=True)
        
        # 發送所有基本信息
        basic_count = 0
        subnet_results_by_netuid = {}
        for result in fast_results:
            if result and not isinstance(result, Exception):
                subnet_results_by_netuid[result['netuid']] = result
                yield json.dumps({"type": "subnet", "data": result}) + "\n"
                basic_count += 1
        
        logger.info(f"基本信息發送完成：{basic_count}個")
        yield json.dumps({"type": "basic_complete"}) + "\n"
        
        # ========== 第二階段：並行獲取metagraph並逐個更新 ==========
        logger.info("第二階段：並行獲取metagraph數據...")
        
        async def metagraph_with_semaphore(netuid):
            async with semaphore:
                return await process_single_subnet_metagraph(subtensor, netuid)
        
        metagraph_tasks = [metagraph_with_semaphore(netuid) for netuid in subnet_netuids]
        
        # 使用 as_completed 逐個返回完成的任務結果
        update_count = 0
        for task in asyncio.as_completed(metagraph_tasks):
            try:
                result = await task
                if result and not isinstance(result, Exception):
                    subnet_results_by_netuid[result['netuid']] = {
                        **subnet_results_by_netuid.get(result['netuid'], {}),
                        **result
                    }
                    set_cached_subnet_metrics(subnet_results_by_netuid[result['netuid']])
                    yield json.dumps({"type": "subnet_update", "data": result}) + "\n"
                    update_count += 1
            except Exception as e:
                logger.warning(f"獲取metagraph更新時出錯: {e}")
        
        logger.info(f"metagraph數據獲取完成：{update_count}個更新")
        set_cached_subnets_snapshot(list(subnet_results_by_netuid.values()))
        
        # 發送完成信號
        yield json.dumps({"type": "complete", "basic": basic_count, "updated": update_count}) + "\n"
        
    except Exception as e:
        logger.error(f"流式API錯誤: {type(e).__name__}: {e}", exc_info=True)
        yield json.dumps({"type": "error", "message": str(e)}) + "\n"


@app.get("/api/subnets-stream")
async def stream_subnets(min_invest_value: float = 0.0, max_invest_value: float = 1.0):
    """流式獲取subnet數據（逐個返回）"""
    return StreamingResponse(
        generate_subnets_stream(min_invest_value, max_invest_value),
        media_type="application/x-ndjson"
    )


@app.get("/api/subnet/{netuid}")
async def get_subnet_detail(netuid: int):
    """獲取特定subnet的詳細信息"""
    try:
        subtensor = await get_subtensor_async()
        
        subnet_data = await process_single_subnet(subtensor, netuid)
        
        if subnet_data is None:
            raise HTTPException(status_code=404, detail=f"Subnet {netuid} 不存在")
        
        return JSONResponse(subnet_data)
        
    except Exception as e:
        logger.error(f"API錯誤: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """健康檢查"""
    return JSONResponse({"status": "ok"})


@app.post("/api/website/stop")
async def stop_website():
    """停止目前網站程序，連帶結束其 background threads 和直接子程序。"""
    threading.Thread(target=_stop_website_process, daemon=True).start()
    return JSONResponse({
        "success": True,
        "message": "網站停止程序已啟動"
    })


@app.get("/api/diagnostic")
async def diagnostic():
    """診斷API - 檢查連接和測試單個subnet"""
    diagnostics = {
        "status": "ok",
        "subtensor_connection": "unknown",
        "sample_subnets": [],
        "errors": []
    }
    
    try:
        logger.info("開始診斷測試...")
        subtensor = await get_subtensor_async()
        diagnostics["subtensor_connection"] = "success"
        
        all_subnets = subtensor.get_all_subnets_info()
        if isinstance(all_subnets, list):
            subnet_netuids = [subnet.netuid for subnet in all_subnets] if all_subnets else []
        else:
            subnet_netuids = list(all_subnets.keys()) if all_subnets else []
        
        logger.info(f"找到{len(subnet_netuids)}個subnets，測試前3個...")
        
        for netuid in subnet_netuids[:3]:
            try:
                result = await process_single_subnet(subtensor, netuid)
                if result:
                    diagnostics["sample_subnets"].append({
                        "netuid": netuid,
                        "status": "success",
                        "name": result.get('name', 'N/A'),
                        "n_miners": result.get('n_miners', 0),
                        "n_validators": result.get('n_validators', 0)
                    })
                else:
                    diagnostics["sample_subnets"].append({
                        "netuid": netuid,
                        "status": "failed",
                        "reason": "process_single_subnet returned None"
                    })
            except Exception as e:
                diagnostics["sample_subnets"].append({
                    "netuid": netuid,
                    "status": "error",
                    "error": str(e)
                })
                diagnostics["errors"].append(f"Subnet {netuid}: {type(e).__name__}: {e}")
        
    except Exception as e:
        logger.error(f"診斷測試失敗: {e}", exc_info=True)
        diagnostics["status"] = "error"
        diagnostics["subtensor_connection"] = "failed"
        diagnostics["errors"].append(str(e))
    
    return JSONResponse(diagnostics)


@app.get("/api/cache/status")
async def cache_status():
    """查看快取狀態"""
    if not REDIS_ENABLED:
        return JSONResponse({
            "enabled": False,
            "message": "Redis 未啟用"
        })
    
    try:
        info = redis_client.info()
        keys_count = redis_client.dbsize()
        
        # 統計各類快取
        metagraph_keys = len(redis_client.keys("metagraph:*"))
        reg_cost_keys = len(redis_client.keys("reg_cost:*"))
        subnet_info_keys = len(redis_client.keys("subnet_info:*"))
        subnet_metrics_keys = len(redis_client.keys(f"{SUBNET_METRICS_CACHE_PREFIX}:*"))
        snapshot_keys = len(redis_client.keys("subnets_snapshot:*"))
        
        return JSONResponse({
            "enabled": True,
            "total_keys": keys_count,
            "metagraph_cached": metagraph_keys,
            "registration_cost_cached": reg_cost_keys,
            "subnet_info_cached": subnet_info_keys,
            "subnet_metrics_cached": subnet_metrics_keys,
            "subnets_snapshot_cached": snapshot_keys,
            "memory_used": info.get('used_memory_human', 'N/A'),
            "ttl_config": {
                "basic_info": CACHE_TTL_BASIC,
                "metagraph": CACHE_TTL_METAGRAPH,
                "subnet_metrics": CACHE_TTL_METAGRAPH,
                "subnets_snapshot": CACHE_TTL_SNAPSHOT
            }
        })
    except Exception as e:
        logger.error(f"無法獲取快取狀態: {e}")
        return JSONResponse({
            "enabled": False,
            "error": str(e)
        })


@app.post("/api/cache/clear")
async def clear_all_cache(pattern: str = "*"):
    """清除快取"""
    if not REDIS_ENABLED:
        return JSONResponse({
            "enabled": False,
            "message": "Redis 未啟用"
        })
    
    try:
        cleared = clear_cache(pattern)
        logger.info(f"已清除 {cleared} 個快取項目（模式: {pattern}）")
        return JSONResponse({
            "success": True,
            "cleared_count": cleared,
            "pattern": pattern
        })
    except Exception as e:
        logger.error(f"清除快取失敗: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        })


@app.post("/api/cache/clear-subnet/{netuid}")
async def clear_subnet_cache(netuid: int):
    """清除特定subnet的快取"""
    if not REDIS_ENABLED:
        return JSONResponse({
            "enabled": False,
            "message": "Redis 未啟用"
        })
    
    try:
        # 清除該 subnet 的所有快取
        patterns = [
            f"metagraph:{netuid}",
            f"reg_cost:{netuid}",
            f"subnet_info:{netuid}",
            get_subnet_metrics_cache_key(netuid),
            SUBNETS_SNAPSHOT_CACHE_KEY
        ]
        total_cleared = 0
        for pattern in patterns:
            keys = redis_client.keys(pattern)
            if keys:
                total_cleared += redis_client.delete(*keys)
        
        logger.info(f"已清除 Subnet {netuid} 的 {total_cleared} 個快取項目")

        return JSONResponse({
            "success": True,
            "netuid": netuid,
            "cleared_count": total_cleared
        })
    except Exception as e:
        logger.error(f"清除 Subnet {netuid} 快取失敗: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        })


# ==================== 啟動應用 ====================

if __name__ == "__main__":
    # 掛載靜態文件
    app.mount("/static", StaticFiles(directory="static"), name="static")
    
    # 啟動服務器
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=9999,
        log_level="info",
        log_config=build_uvicorn_log_config()
    )
