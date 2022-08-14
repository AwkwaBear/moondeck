from enum import Enum
from typing import Optional
from lib.moonlightproxy import MoonlightProxy
from lib.steambuddyclient import SteamBuddyClient
from lib.logger import logger, set_log_filename
from lib.settings import settings_manager

import os
import asyncio
import lib.constants as constants
import lib.hostinfo as hostinfo
import lib.inmsgs as inmsgs
import lib.runnerresult as runnerresult

set_log_filename(constants.RUNNER_LOG_FILE)


class SpecialHandling(Enum):
    AppFinishedUpdating = "App is forever updating..."


async def pool_steam_status(client: SteamBuddyClient, predicate):
    while True:
        status = await client.get_steam_status(timeout=constants.DEFAULT_TIMEOUT)
        if not isinstance(status, inmsgs.SteamStatus):
            return status

        result = await predicate(status)
        if isinstance(result, bool) or result is None:
            if result:
                return None
        else:
            return result


async def wait_for_app_to_close(client: SteamBuddyClient, app_id: int):
    async def wait_till_close(status: inmsgs.SteamStatus):
        if status.running_app_id != app_id or not status.steam_is_running:
            return True

        await asyncio.sleep(1)

    return await pool_steam_status(client, wait_till_close)


async def wait_for_app_launch(client: SteamBuddyClient, app_id: int):
    initial_retry_count = 30
    obj = {"retries": initial_retry_count, "was_updating": False}

    async def wait_till_launch(status: inmsgs.SteamStatus):
        if status.steam_is_running:
            if status.running_app_id == app_id:
                return True
            if status.last_launched_app_is_updating == app_id:
                obj["was_updating"] = True
                await asyncio.sleep(1)
                return False
            if obj["was_updating"]:
                return SpecialHandling.AppFinishedUpdating

        obj["retries"] -= 1
        if obj["retries"] > 0:
            await asyncio.sleep(1)
            return False
        else:
            return runnerresult.Result.AppLaunchFailed

    return await pool_steam_status(client, wait_till_launch)


async def wait_for_steam_to_be_ready(client: SteamBuddyClient, app_id: int):
    initial_retry_count = 15
    obj = {"retries": initial_retry_count, "counter": 0}

    async def wait_till_ready(status: inmsgs.SteamStatus):
        valid_app_ids = [constants.NVIDIA_STEAM_APP_ID, app_id]
        if status.steam_is_running:
            if status.last_launched_app_is_updating == app_id:
                obj["retries"] = initial_retry_count
                await asyncio.sleep(1)
                return False

            if status.running_app_id in valid_app_ids:
                if obj["counter"] == 0:
                    obj["retries"] = initial_retry_count
                if obj["counter"] > 5:
                    return True
                obj["counter"] += 1
                await asyncio.sleep(0.5)
                return False

        # NULL id means that the steam is still launching so lets be more lenient
        obj["retries"] -= 1 * (0.25 if constants.NULL_STEAM_APP_ID else 1)
        obj["counter"] = 0
        if obj["retries"] > 0:
            await asyncio.sleep(1)
            return False
        else:
            return runnerresult.Result.AnotherSteamAppIsRunning

    return await pool_steam_status(client, wait_till_ready)


async def launch_app_and_wait(client: SteamBuddyClient, app_id: int):
    logger.info("Waiting for GameStream to start streaming Steam")
    retries = 30
    while retries:
        server_info = await hostinfo.get_server_info(client.address, timeout=constants.DEFAULT_TIMEOUT)
        if not server_info:
            return runnerresult.Result.GameStreamDead

        if server_info["currentGame"] == constants.GAMESTREAM_STEAM_ID:
            break

        retries -= 1
        await asyncio.sleep(0 if not retries else 1)

    if not retries:
        return runnerresult.Result.SteamLaunchFailed

    logger.info("Waiting for Steam to be ready to launch games")
    result = await wait_for_steam_to_be_ready(client, app_id)
    if result:
        return result

    retries = 5
    result = SpecialHandling.AppFinishedUpdating
    while result == SpecialHandling.AppFinishedUpdating and retries:
        retries -= 1

        logger.info(f"Sending request to launch app {app_id}")
        result = await client.launch_app(app_id, timeout=constants.DEFAULT_TIMEOUT)
        if result:
            return result

        logger.info(f"Waiting for app {app_id} to be launched in Steam")
        result = await wait_for_app_launch(client, app_id)
        if result and result != SpecialHandling.AppFinishedUpdating:
            return result 

    if result:
        logger.info(f"Giving up waiting for {app_id} to finish updating cycles")
        return result    

    logger.info("Waiting for app or Steam to close")
    result = await wait_for_app_to_close(client, app_id)
    if result:
        return result

    logger.info(
        "App closed gracefully, asking to close Steam if it's still open")
    result = await client.close_steam(timeout=constants.DEFAULT_TIMEOUT)
    if result:
        return result


async def start_moonlight(proxy: MoonlightProxy):
    logger.info("Checking if Moonlight flatpak is installed")
    if not await proxy.is_moonlight_installed():
        return runnerresult.Result.MoonlightIsNotInstalled

    logger.info("Terminating all Moonlight instances if any")
    await proxy.terminate_all_instances()

    logger.info("Starting Moonlight")
    await proxy.start()


async def establish_connection(client: SteamBuddyClient):
    logger.info("Establishing connection to Buddy")
    resp = await client.login(timeout=constants.DEFAULT_TIMEOUT)
    if resp:
        return resp

    logger.info("Quering GameStream for running games")
    server_info = await hostinfo.get_server_info(client.address, timeout=constants.DEFAULT_TIMEOUT)
    if not server_info:
        return runnerresult.Result.GameStreamDead

    if server_info["currentGame"] not in [constants.GAMESTREAM_STEAM_ID, constants.GAMESTREAM_IDLE_ID]:
        return runnerresult.Result.GameStreamBusy

    is_steam_running = server_info["currentGame"] == constants.GAMESTREAM_STEAM_ID
    if is_steam_running:
        logger.info("Steam is already being streamed...")

    return None


async def run_game(hostname: str, address: str, port: int, client_id: Optional[str], app_id: int):
    proxy = MoonlightProxy(hostname)
    client = SteamBuddyClient(address, port, client_id)
    try:
        result = await establish_connection(client=client)
        if result:
            return result

        result = await start_moonlight(proxy=proxy)
        if result:
            return result

        proxy_task = asyncio.create_task(proxy.wait())
        launch_task = asyncio.create_task(
            launch_app_and_wait(client=client, app_id=app_id))

        done, _ = await asyncio.wait({proxy_task, launch_task}, return_when=asyncio.FIRST_COMPLETED)
        if proxy_task in done:
            done, _ = await asyncio.wait({launch_task}, timeout=2)
            if launch_task in done:
                result = launch_task.result()
                if result:
                    return result
            else:
                launch_task.cancel()
                await asyncio.wait({launch_task}, timeout=2)
                return runnerresult.Result.MoonlightClosed
        else:
            assert launch_task in done, "Launch task is not done?!"
            result = launch_task.result()
            if result:
                return result

    except Exception:
        logger.exception("Unhandled exception")
        return runnerresult.Result.Exception

    finally:
        if proxy:
            await proxy.terminate()
        if client:
            await client.disconnect()


def get_app_id() -> Optional[int]:
    app_id = os.environ.get("MOONDECK_STEAM_APP_ID")
    try:
        return int(app_id) if app_id is not None else app_id
    except:
        logger.exception("While getting app id")
        return None


async def main():
    try:
        logger.info("Resetting runner result")
        runnerresult.set_result(
            runnerresult.Result.ClosedPrematurely, log_result=False)

        logger.info("Getting app id")
        app_id = get_app_id()
        if app_id is None:
            runnerresult.set_result(runnerresult.Result.NoAppId)
            return

        logger.info("Getting current host settings")
        user_settings = await settings_manager.get()
        host_settings = None

        host_id = user_settings["currentHostId"]
        if host_id is not None and host_id in user_settings["hostSettings"]:
            host_settings = user_settings["hostSettings"][host_id]

        if host_settings is None:
            runnerresult.set_result(runnerresult.Result.HostNotSelected)
            return

        logger.info("Trying to run the game")
        result = await run_game(host_settings["hostName"],
                                host_settings["address"],
                                host_settings["buddyPort"],
                                user_settings["clientId"],
                                app_id)
        runnerresult.set_result(result)

    except Exception:
        logger.exception("Unhandled exception")
        runnerresult.set_result(runnerresult.Result.Exception)


if __name__ == "__main__":
    asyncio.run(main())
