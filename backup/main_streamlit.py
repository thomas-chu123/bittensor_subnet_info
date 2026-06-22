import streamlit as st
import bittensor as bt
from bittensor_cli.src.bittensor.balances import Balance
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
import time
from functools import lru_cache

# ==================== 配置 ====================
NETWORK = 'finney'
SUBTENSOR_NETWORK = 'finney'

st.set_page_config(
    page_title="Bittensor Subnet Info",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded"
)

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


def calculate_invest_value(incentives: np.ndarray) -> float:
    """
    計算投資價值 (0~1)
    基於incentive分佈的均勻性
    0.0 = 分佈極度不均勻（風險高）
    1.0 = 分佈均勻（風險低）
    """
    if len(incentives) == 0 or np.sum(incentives) == 0:
        return 0.5
    
    gini = calculate_gini_coefficient(incentives)
    # 反轉Gini係數：越均勻越高價值
    invest_value = 1.0 - gini
    return float(invest_value)


@st.cache_resource
def get_subtensor():
    """連接到Subtensor網絡"""
    try:
        return bt.Subtensor(network=SUBTENSOR_NETWORK)
    except Exception as e:
        st.error(f"無法連接到Subtensor: {e}")
        return None


def get_all_subnets() -> List[int]:
    """獲取所有subnet的netuid"""
    try:
        subtensor = get_subtensor()
        if subtensor is None:
            return []
        
        # 獲取所有subnet的netuids
        subnets = subtensor.get_all_subnets_info()
        # subnets是列表，直接返回或轉換為netuid列表
        if isinstance(subnets, list):
            return [subnet.netuid for subnet in subnets] if subnets else []
        else:
            return list(subnets.keys()) if subnets else []
    except Exception as e:
        st.error(f"無法獲取subnet列表: {e}")
        return []


def get_subnet_registration_cost(subtensor, netuid: int) -> float:
    """獲取subnet的registration cost"""
    try:
        # 嘗試方式1: 使用get_hyperparameter（參考subnets.py的show_subnet實現）
        try:
            burn_cost_rao = subtensor.get_hyperparameter(
                param_name="Burn", 
                netuid=netuid
            )
            if burn_cost_rao is not None:
                burn_cost = Balance.from_rao(int(burn_cost_rao))
                return float(burn_cost.tao)
        except:
            pass
        
        # 嘗試方式2: 使用get_burn_rate
        try:
            burn_rate = subtensor.get_burn_rate(netuid=netuid)
            if burn_rate is not None:
                return float(burn_rate)
        except:
            pass
        
        # 方式3: 直接返回0（無法獲取時）
        return 0.0
        
    except Exception as e:
        st.warning(f"無法獲取Subnet {netuid}的registration cost: {e}")
        return 0.0


def get_metagraph_data(netuid: int, lite: bool = True) -> Dict:
    """獲取subnet的metagraph數據"""
    try:
        metagraph = bt.Metagraph(netuid=netuid, lite=lite, network=NETWORK)
        
        # 準備數據
        data = {
            'netuid': netuid,
            'n': len(metagraph.hotkeys),
            'hotkeys': metagraph.hotkeys,
            'incentives': metagraph.incentive.cpu().numpy() if hasattr(metagraph.incentive, 'cpu') else np.array(metagraph.incentive),
            'stakes': metagraph.stake.cpu().numpy() if hasattr(metagraph.stake, 'cpu') else np.array(metagraph.stake),
            'emissions': metagraph.emission.cpu().numpy() if hasattr(metagraph.emission, 'cpu') else np.array(metagraph.emission),
            'dividends': metagraph.dividends.cpu().numpy() if hasattr(metagraph.dividends, 'cpu') else np.array(metagraph.dividends),
        }
        
        return data
    except Exception as e:
        st.error(f"無法獲取Subnet {netuid}的metagraph: {e}")
        return None


def get_subnet_info(subtensor, netuid: int) -> Dict:
    """獲取subnet的基本信息"""
    try:
        subnet_info = subtensor.get_subnet_info(netuid)
        if subnet_info is None:
            return {}
        
        return {
            'name': getattr(subnet_info, 'name', 'N/A'),
            'owner': getattr(subnet_info, 'owner', 'N/A'),
            'max_allowed_validators': getattr(subnet_info, 'max_allowed_validators', 'N/A'),
            'total_stake': float(getattr(subnet_info, 'total_stake', 0)) / 1e9,
            'total_issuance': float(getattr(subnet_info, 'total_issuance', 0)) / 1e9,
        }
    except Exception as e:
        st.warning(f"無法獲取Subnet {netuid}的信息: {e}")
        return {}


def format_tao(value: float) -> str:
    """將值格式化為TAO"""
    if value >= 1000:
        return f"{value / 1000:.2f}k τ"
    elif value >= 1:
        return f"{value:.2f} τ"
    else:
        return f"{value * 1000:.2f}m τ"


# ==================== 主應用 ====================

def main():
    st.title("🧠 Bittensor Subnet 信息儀表盤")
    st.markdown("---")
    
    # 側邊欄配置
    with st.sidebar:
        st.header("⚙️ 設置")
        
        refresh_interval = st.selectbox(
            "刷新間隔",
            options=[5, 10, 30, 60],
            index=1,
            help="自動刷新數據的間隔（秒）"
        )
        
        show_details = st.checkbox(
            "顯示詳細數據",
            value=False,
            help="顯示metagraph的詳細信息"
        )
        
        filter_by_invest_value = st.slider(
            "投資價值過濾",
            min_value=0.0,
            max_value=1.0,
            value=(0.0, 1.0),
            step=0.05,
            help="只顯示符合投資價值範圍的subnet"
        )
        
        if st.button("🔄 刷新數據", key="refresh_button"):
            st.cache_resource.clear()
            st.rerun()
    
    # 主要內容
    subtensor = get_subtensor()
    if subtensor is None:
        st.error("無法連接到Subtensor網絡")
        return
    
    # 獲取所有subnet
    st.info("📡 正在加載subnet列表...")
    with st.spinner("獲取subnet數據..."):
        all_subnets = get_all_subnets()
    
    if not all_subnets:
        st.error("無法獲取subnet列表")
        return
    
    st.success(f"✅ 已加載 {len(all_subnets)} 個subnet")
    
    # 建立subnet數據集合
    st.info("📊 正在分析各個subnet...")
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    subnet_data_list = []
    
    for idx, netuid in enumerate(all_subnets):
        status_text.text(f"正在加載Subnet {netuid}... ({idx + 1}/{len(all_subnets)})")
        progress_bar.progress((idx + 1) / len(all_subnets))
        
        try:
            # 獲取metagraph數據
            metagraph_data = get_metagraph_data(netuid, lite=True)
            if metagraph_data is None:
                continue
            
            # 獲取registration cost
            reg_cost = get_subnet_registration_cost(subtensor, netuid)
            
            # 獲取subnet基本信息
            subnet_info = get_subnet_info(subtensor, netuid)
            
            # 計算投資價值
            incentives = np.array(metagraph_data['incentives'])
            invest_value = calculate_invest_value(incentives)
            
            # 計算incentive統計信息
            incentive_mean = float(np.mean(incentives)) if len(incentives) > 0 else 0.0
            incentive_std = float(np.std(incentives)) if len(incentives) > 0 else 0.0
            
            # 計算總stake和emissions
            total_stake = float(np.sum(metagraph_data['stakes'])) / 1e9
            total_emissions = float(np.sum(metagraph_data['emissions'])) / 1e9
            
            subnet_data = {
                'netuid': netuid,
                'name': subnet_info.get('name', 'N/A'),
                'n_validators': metagraph_data['n'],
                'registration_cost': reg_cost,
                'invest_value': invest_value,
                'total_stake': total_stake,
                'total_emissions': total_emissions,
                'incentive_mean': incentive_mean,
                'incentive_std': incentive_std,
                'owner': subnet_info.get('owner', 'N/A'),
                'metagraph_data': metagraph_data,
            }
            
            subnet_data_list.append(subnet_data)
            
        except Exception as e:
            st.warning(f"處理Subnet {netuid}時出錯: {e}")
            continue
    
    progress_bar.empty()
    status_text.empty()
    
    if not subnet_data_list:
        st.error("無法加載任何subnet數據")
        return
    
    # 過濾subnet
    filtered_subnets = [
        s for s in subnet_data_list
        if filter_by_invest_value[0] <= s['invest_value'] <= filter_by_invest_value[1]
    ]
    
    st.success(f"✅ 已加載 {len(filtered_subnets)} 個符合條件的subnet")
    st.markdown("---")
    
    # 建立數據框架用於顯示
    df_display = pd.DataFrame([
        {
            'Subnet ID': s['netuid'],
            'Name': s['name'],
            'Validators': s['n_validators'],
            'Registration Cost (τ)': f"{s['registration_cost']:.2f}",
            'Invest Value': f"{s['invest_value']:.2f}",
            'Total Stake (τ)': f"{format_tao(s['total_stake'])}",
            'Total Emissions (τ)': f"{format_tao(s['total_emissions'])}",
            'Incentive Mean': f"{s['incentive_mean']:.4f}",
            'Incentive Std': f"{s['incentive_std']:.4f}",
        }
        for s in filtered_subnets
    ])
    
    # 顯示主表格
    st.subheader("📊 Subnet 概覽")
    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True
    )
    
    # 按投資價值排序
    sorted_subnets = sorted(filtered_subnets, key=lambda x: x['invest_value'], reverse=True)
    
    # 顯示頂部推薦subnet
    st.subheader("⭐ 投資價值最高的Subnets")
    
    if len(sorted_subnets) > 0:
        top_n = min(5, len(sorted_subnets))
        cols = st.columns(top_n)
        
        for col, subnet in zip(cols, sorted_subnets[:top_n]):
            with col:
                st.metric(
                    label=f"Subnet {subnet['netuid']}",
                    value=f"{subnet['invest_value']:.2f}",
                    delta=f"{subnet['registration_cost']:.2f} τ",
                )
                st.caption(f"Name: {subnet['name']}")
                st.caption(f"Validators: {subnet['n_validators']}")
                
                # Deploy buttons（Phase 2）
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("🌐 SSH", key=f"ssh_{subnet['netuid']}", disabled=True):
                        st.info("SSH Deploy coming in Phase 2")
                with col2:
                    if st.button("☁️ RunPod", key=f"runpod_{subnet['netuid']}", disabled=True):
                        st.info("RunPod Deploy coming in Phase 2")
    
    # 詳細信息（如果啟用）
    if show_details:
        st.markdown("---")
        st.subheader("📈 詳細分析")
        
        for subnet in filtered_subnets[:3]:  # 只顯示前3個以避免頁面過長
            with st.expander(f"Subnet {subnet['netuid']} - {subnet['name']} (投資價值: {subnet['invest_value']:.2f})"):
                
                metagraph_data = subnet['metagraph_data']
                
                # 基本信息
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Validators", subnet['n_validators'])
                with col2:
                    st.metric("Registration Cost", f"{subnet['registration_cost']:.2f} τ")
                with col3:
                    st.metric("Total Stake", f"{format_tao(subnet['total_stake'])}")
                with col4:
                    st.metric("Total Emissions", f"{format_tao(subnet['total_emissions'])}")
                
                # Incentive分佈
                incentives = np.array(metagraph_data['incentives'])
                
                st.write("**Incentive 分佈統計:**")
                stats_col1, stats_col2, stats_col3, stats_col4 = st.columns(4)
                with stats_col1:
                    st.metric("Mean", f"{np.mean(incentives):.4f}")
                with stats_col2:
                    st.metric("Std Dev", f"{np.std(incentives):.4f}")
                with stats_col3:
                    st.metric("Min", f"{np.min(incentives):.4f}")
                with stats_col4:
                    st.metric("Max", f"{np.max(incentives):.4f}")
                
                # Incentive直方圖
                st.write("**Incentive 分佈直方圖:**")
                hist_data = pd.DataFrame({
                    'Incentive': incentives
                })
                st.bar_chart(hist_data['Incentive'].hist(bins=50), use_container_width=True)
    
    # 頁面底部信息
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("總Subnet數", len(all_subnets))
    with col2:
        st.metric("顯示Subnet數", len(filtered_subnets))
    with col3:
        st.metric("最後更新", time.strftime("%Y-%m-%d %H:%M:%S"))
    
    st.markdown("---")
    st.caption("💡 提示: 投資價值 = 1 - Gini系數 (incentive分佈的均勻性)")
    st.caption("🔒 Phase 2: 將添加SSH和RunPod一鍵部署功能")


if __name__ == "__main__":
    main()
