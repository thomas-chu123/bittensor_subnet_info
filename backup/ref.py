import streamlit as st
import bittensor as bt
import pandas as pd
import wandb

PUBLIC_WANDB_NAME = "opencompute"
PUBLIC_WANDB_ENTITY = "neuralinternet"
NETID = 27
NETWORK = 'finney'

def display_meta():
    # Initialize metagraph with your specific netid

    metagraph = bt.metagraph(NETID, lite=True, network = NETWORK)
    miner_version_summary = {}
    validator_version_summary = {}

    # Prepare data for display
    data = []

    for hotkey in metagraph.hotkeys:
        index = metagraph.hotkeys.index(hotkey)
        axon = metagraph.axons[index]
        stake = metagraph.stake[index]
        trust = metagraph.trust[index]
        v_trust = metagraph.validator_trust[index]
        v_permit = metagraph.validator_permit[index]
        active = metagraph.active[index]
        if active == 0:
            data.append([index, hotkey, active, stake, trust, v_permit, v_trust, axon.ip, axon.port, axon.version])
            if axon.version in miner_version_summary:
                miner_version_summary[axon.version] += 1
            else:
                miner_version_summary[axon.version] = 1
        else:
            val_version = get_validator_version(hotkey=hotkey)
            data.append([index, hotkey, active, stake, trust, v_permit, v_trust, axon.ip, axon.port, val_version])
            if val_version in validator_version_summary:
                validator_version_summary[val_version] += 1
            else:
                validator_version_summary[val_version] = 1

    # Convert to DataFrame for display
    columns = ['UID', 'Hotkey', 'Active', 'Stake', 'Trust', 'V_Permit', 'V_Trust', 'IP', 'Port', 'Version']
    df = pd.DataFrame(data, columns=columns)

    # Streamlit UI
    st.title('Subnet 27 Metagraph Data Summary')
    st.write('### Metagraph Nodes Data')
    st.dataframe(df)

    # Validator version summary
    validator_count = sum(validator_version_summary.values())
    validator_summary_data = [
        {'Version': version, 'Count': count, 'Percentage': f"{count / validator_count * 100:.2f}%"}
        for version, count in validator_version_summary.items()
    ]
    validator_summary_df = pd.DataFrame(validator_summary_data)

    st.write('### Validator Version Summary')
    st.write(f'Total Validator Count: {validator_count}')
    st.dataframe(validator_summary_df)

    # Miner version summary
    miner_count = sum(miner_version_summary.values())
    miner_summary_data = [
        {'Version': version, 'Count': count, 'Percentage': f"{count / miner_count * 100:.2f}%"}
        for version, count in miner_version_summary.items()
    ]
    miner_summary_df = pd.DataFrame(miner_summary_data)

    st.write('### Miner Version Summary')
    st.write(f'Total Miner Count: {miner_count}')
    st.dataframe(miner_summary_df)

def get_validator_version(hotkey):
    """
    This function gets version from validator.
    Only relevant for validators.
    """
    # Dictionary to store the (hotkey, specs) from wandb runs
    db_specs_dict = {}

    api = wandb.Api()
    runs = api.runs(f"{PUBLIC_WANDB_ENTITY}/{PUBLIC_WANDB_NAME}",
                        filters={"$and": [{"config.role": "validator"},
                                          {"config.config.netuid": NETID},
                                          {"config.hotkey": hotkey},
                                          {"state": "running"},]
                                })
    try:
        version = 0
        # Iterate over all runs in the opencompute project
        for index, run in enumerate(runs, start=1):
            # Access the run's configuration
            run_config = run.config
            version = run_config.get('version', 0)
        print(f"Hotkey: {hotkey}, Version: {version}")
        return version
    except Exception as e:
        # Handle the exception by logging an error message
        print(f"An error occurred while getting specs from wandb: {e}")

if __name__ == '__main__':
    display_meta()
