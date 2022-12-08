import { setShortcutName, terminateApp } from "./steamutils";
import { BehaviorSubject } from "rxjs";
import { CommandProxy } from "./commandproxy";
import { ReadonlySubject } from "./readonlysubject";
import { ServerAPI } from "decky-frontend-lib";
import { isEqual } from "lodash";
import { logger } from "./logger";

async function killRunner(serverAPI: ServerAPI, appId: number): Promise<void> {
  try {
    const resp = await serverAPI.callPluginMethod<{ app_id: number }, string>("kill_runner", { app_id: appId });
    if (!resp.success) {
      logger.error(`Error while killing runner script: ${resp.result}`);
    }
  } catch (message) {
    logger.critical(message);
  }
}

export interface SessionOptions {
  nameSetToAppId: boolean;
}

export interface MoonDeckAppData {
  steamAppId: number;
  moonDeckAppId: number;
  name: string;
  redirected: boolean;
  beingKilled: boolean;
  beingSuspended: boolean;
  sessionOptions: SessionOptions;
}

export class MoonDeckAppProxy extends ReadonlySubject<MoonDeckAppData | null> {
  private readonly serverAPI: ServerAPI;
  private readonly commandProxy: CommandProxy;

  constructor(serverAPI: ServerAPI, commandProxy: CommandProxy) {
    super(new BehaviorSubject<MoonDeckAppData | null>(null));
    this.serverAPI = serverAPI;
    this.commandProxy = commandProxy;
  }

  setApp(steamAppId: number, moonDeckAppId: number, name: string, sessionOptions: SessionOptions): void {
    this.subject.next({
      steamAppId,
      moonDeckAppId,
      name,
      redirected: false,
      beingKilled: false,
      beingSuspended: false,
      sessionOptions
    });
  }

  async applySessionOptions(): Promise<void> {
    const options = this.subject.value?.sessionOptions ?? null;
    if (options === null) {
      return;
    }

    if (!await this.changeName(options.nameSetToAppId)) {
      logger.toast("Failed to change shortcut name!", { output: "warn" });
    }
  }

  async changeName(changeToAppId: boolean): Promise<boolean> {
    if (this.subject.value === null) {
      return false;
    }

    let result = true;
    if (changeToAppId) {
      result = await setShortcutName(this.subject.value.moonDeckAppId, `${this.subject.value.steamAppId}`);
    } else {
      result = await setShortcutName(this.subject.value.moonDeckAppId, this.subject.value.name);
    }

    if (result) {
      const newValue = { ...this.subject.value, sessionOptions: { ...this.subject.value.sessionOptions, nameSetToAppId: changeToAppId } };
      if (!isEqual(this.subject.value, newValue)) {
        this.subject.next(newValue);
      }
    }

    return result;
  }

  canRedirect(): boolean {
    if (this.subject.value === null || this.subject.value.redirected) {
      return false;
    }

    this.subject.next({ ...this.subject.value, redirected: true });
    return true;
  }

  async clearApp(): Promise<void> {
    if (this.subject.value === null) {
      return;
    }

    await this.changeName(false);
    this.subject.next(null);
  }

  async closeSteamOnHost(): Promise<void> {
    if (this.subject.value === null) {
      return;
    }

    await this.commandProxy.closeSteam(false);
  }

  async killApp(): Promise<void> {
    if (this.subject.value === null) {
      return;
    }

    this.subject.next({ ...this.subject.value, beingKilled: true });

    const nameSetToAppId = this.subject.value.sessionOptions.nameSetToAppId;
    // Necessary, otherwise the termination fails
    await this.changeName(false);
    if (!await terminateApp(this.subject.value.moonDeckAppId, 5000)) {
      logger.toast("Failed to terminate, trying to kill!", { output: "warn" });
      await killRunner(this.serverAPI, this.subject.value.moonDeckAppId);
    }

    // Reset the original value
    if (nameSetToAppId) {
      const newValue = { ...this.subject.value, sessionOptions: { ...this.subject.value.sessionOptions, nameSetToAppId } };
      if (!isEqual(this.subject.value, newValue)) {
        this.subject.next(newValue);
      }
    }
  }

  async suspendApp(): Promise<void> {
    if (this.subject.value === null) {
      return;
    }

    this.subject.next({ ...this.subject.value, beingSuspended: true });
    await this.killApp();
  }
}
