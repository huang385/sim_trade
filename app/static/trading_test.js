"use strict";

const REFRESH_INTERVAL_MS = 500;
const SLOW_REFRESH_INTERVAL_MS = 2000;
const ACTIVE_ORDER_STATUSES = new Set(["ACCEPTED", "PARTIALLY_FILLED"]);

const state = {
    accountId: "",
    accessToken: null,
    currentUser: null,
    positions: [],
    refreshing: false,
    refreshTimer: null,
    slowRefreshTimer: null,
    websocket: null,
    websocketConnected: false,
    websocketReconnectTimer: null,
    websocketReconnectAttempt: 0,
    websocketManualClose: false,
    account: null,
    pnl: null,
    orders: [],
    trades: [],
    websocketSubscribedAccounts: new Set(),
};

const elements = {
    loginPanel: document.querySelector("#login-panel"),
    loginForm: document.querySelector("#login-form"),
    loginUsername: document.querySelector("#login-username"),
    loginPassword: document.querySelector("#login-password"),
    loginButton: document.querySelector("#login-button"),
    sessionPanel: document.querySelector("#session-panel"),
    accountToolbar: document.querySelector("#account-toolbar"),
    currentUser: document.querySelector("#current-user"),
    logoutButton: document.querySelector("#logout-button"),
    accountId: document.querySelector("#account-id"),
    orderAccountId: document.querySelector("#order-account-id"),
    loadAccount: document.querySelector("#load-account"),
    refreshNow: document.querySelector("#refresh-now"),
    autoRefresh: document.querySelector("#auto-refresh"),
    connectionDot: document.querySelector("#connection-dot"),
    connectionText: document.querySelector("#connection-text"),
    lastRefresh: document.querySelector("#last-refresh"),
    positionsBody: document.querySelector("#positions-body"),
    ordersBody: document.querySelector("#orders-body"),
    tradesBody: document.querySelector("#trades-body"),
    positionCount: document.querySelector("#position-count"),
    orderCount: document.querySelector("#order-count"),
    tradeCount: document.querySelector("#trade-count"),
    metricEquity: document.querySelector("#metric-equity"),
    metricAvailable: document.querySelector("#metric-available"),
    metricNetPnl: document.querySelector("#metric-net-pnl"),
    metricUnrealized: document.querySelector("#metric-unrealized"),
    metricRealized: document.querySelector("#metric-realized"),
    metricDailyPnl: document.querySelector("#metric-daily-pnl"),
    metricDailyPosition: document.querySelector("#metric-daily-position"),
    metricDailyClose: document.querySelector("#metric-daily-close"),
    metricDailyCommission: document.querySelector("#metric-daily-commission"),
    metricRisk: document.querySelector("#metric-risk"),
    metricSource: document.querySelector("#metric-source"),
    metricAccountStatus: document.querySelector("#metric-account-status"),
    accountName: document.querySelector("#account-name"),
    accountTradingDay: document.querySelector("#account-trading-day"),
    detailInitialCash: document.querySelector("#detail-initial-cash"),
    detailCashBalance: document.querySelector("#detail-cash-balance"),
    detailUsedMargin: document.querySelector("#detail-used-margin"),
    detailFrozenMargin: document.querySelector("#detail-frozen-margin"),
    detailFrozenCash: document.querySelector("#detail-frozen-cash"),
    detailUsedCommission: document.querySelector("#detail-used-commission"),
    detailFrozenCommission: document.querySelector("#detail-frozen-commission"),
    detailAccountType: document.querySelector("#detail-account-type"),
    orderForm: document.querySelector("#order-form"),
    submitOrder: document.querySelector("#submit-order"),
    exchangeId: document.querySelector("#exchange-id"),
    symbol: document.querySelector("#symbol"),
    limitPrice: document.querySelector("#limit-price"),
    volume: document.querySelector("#volume"),
    offsetFlag: document.querySelector("#offset-flag"),
    clientOrderId: document.querySelector("#client-order-id"),
    regenerateId: document.querySelector("#regenerate-id"),
    toastRegion: document.querySelector("#toast-region"),
    tradeDetailDialog: document.querySelector("#trade-detail-dialog"),
    tradeDetailTitle: document.querySelector("#trade-detail-title"),
    tradeDetailBody: document.querySelector("#trade-detail-body"),
    closeTradeDialog: document.querySelector("#close-trade-dialog"),
};

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function asNumber(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
}

function formatMoney(value, digits = 2) {
    return asNumber(value).toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
}

function formatPrice(value) {
    if (value === null || value === undefined || value === "") {
        return "--";
    }
    return asNumber(value).toLocaleString("zh-CN", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 6,
    });
}

function formatTime(value, includeDate = false) {
    if (!value) {
        return "--";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return escapeHtml(value);
    }
    return new Intl.DateTimeFormat("zh-CN", {
        ...(includeDate ? {month: "2-digit", day: "2-digit"} : {}),
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
    }).format(date);
}

function pnlClass(value) {
    const number = asNumber(value);
    if (number > 0) return "positive";
    if (number < 0) return "negative";
    return "";
}

function setMetric(element, value, suffix = "") {
    element.textContent = `${formatMoney(value)}${suffix}`;
    element.classList.remove("positive", "negative");
    if (!suffix) {
        const cssClass = pnlClass(value);
        if (cssClass) element.classList.add(cssClass);
    }
}

async function apiFetch(path, options = {}, allowRefresh = true) {
    const response = await fetch(path, {
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json; charset=utf-8",
            ...(state.accessToken
                ? {Authorization: `Bearer ${state.accessToken}`}
                : {}),
            ...(options.headers || {}),
        },
        ...options,
    });

    if (
        response.status === 401
        && allowRefresh
        && !path.startsWith("/api/auth/")
        && await refreshAccessToken()
    ) {
        return apiFetch(path, options, false);
    }

    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json")
        ? await response.json()
        : await response.text();

    if (!response.ok) {
        const validationMessage = Array.isArray(data?.detail)
            ? data.detail.map((item) => item.msg).join("；")
            : null;
        const message = data?.message
            || validationMessage
            || data?.detail
            || `请求失败（HTTP ${response.status}）`;
        throw new Error(message);
    }
    return data;
}

async function refreshAccessToken() {
    try {
        const result = await apiFetch(
            "/api/auth/refresh",
            {method: "POST"},
            false,
        );
        state.accessToken = result.access_token;
        state.currentUser = result.user;
        return true;
    } catch (_error) {
        showLogin();
        return false;
    }
}

function showLogin() {
    closeRealtimeSocket(true);
    state.accessToken = null;
    state.currentUser = null;
    state.accountId = "";
    restartTimers();
    elements.loginPanel.classList.remove("hidden");
    elements.sessionPanel.classList.add("hidden");
    elements.accountToolbar.classList.add("hidden");
}

async function loadAuthorizedAccounts() {
    const result = await apiFetch("/api/auth/me");
    const accounts = result.accounts || [];
    elements.accountId.innerHTML = accounts.map((account) => (
        `<option value="${escapeHtml(account.account_id)}">`
        + `${escapeHtml(account.account_name)} (${escapeHtml(account.account_id)})`
        + "</option>"
    )).join("");
    state.currentUser = result.user;
    elements.currentUser.textContent = (
        `${result.user.display_name} · ${result.user.role}`
    );
    elements.loginPanel.classList.add("hidden");
    elements.sessionPanel.classList.remove("hidden");
    elements.accountToolbar.classList.remove("hidden");
    if (accounts.length) {
        state.accountId = accounts[0].account_id;
        elements.orderAccountId.value = state.accountId;
        await Promise.all([refreshRealtime(), refreshOrdersAndTrades()]);
        await connectRealtimeSocket();
        restartTimers();
    } else {
        setConnection(false, "当前用户没有可访问的交易账户");
    }
}

async function login(event) {
    event.preventDefault();
    elements.loginButton.disabled = true;
    try {
        const result = await apiFetch(
            "/api/auth/login",
            {
                method: "POST",
                body: JSON.stringify({
                    username: elements.loginUsername.value.trim(),
                    password: elements.loginPassword.value,
                }),
            },
            false,
        );
        state.accessToken = result.access_token;
        elements.loginPassword.value = "";
        await loadAuthorizedAccounts();
        showToast("登录成功");
    } catch (error) {
        showToast(`登录失败：${error.message}`, "error");
    } finally {
        elements.loginButton.disabled = false;
    }
}

async function logout() {
    closeRealtimeSocket(true);
    try {
        await apiFetch(
            "/api/auth/logout",
            {method: "POST"},
            false,
        );
    } finally {
        showLogin();
    }
}

function closeRealtimeSocket(manual = false) {
    state.websocketManualClose = manual;
    window.clearTimeout(state.websocketReconnectTimer);
    state.websocketReconnectTimer = null;
    const socket = state.websocket;
    state.websocket = null;
    if (socket) {
        socket.close(1000, "页面主动关闭");
    }
    state.websocketConnected = false;
    state.websocketSubscribedAccounts.clear();
}

function websocketUrl(ticket) {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    // 第一版Gateway按独立进程部署在8001端口；正式反向代理可把这里改为同源。
    return `${scheme}://${window.location.hostname}:8001/ws/trading?ticket=${encodeURIComponent(ticket)}`;
}

function selectedAccountIds() {
    const selected = Array.from(elements.accountId.selectedOptions || [])
        .map((option) => option.value.trim())
        .filter(Boolean);
    return selected.length ? selected : (state.accountId ? [state.accountId] : []);
}

function subscribeCurrentAccount() {
    if (!state.websocketConnected || !state.accountId) return;
    const accountIds = selectedAccountIds();
    const removed = Array.from(state.websocketSubscribedAccounts).filter(
        (accountId) => !accountIds.includes(accountId),
    );
    if (removed.length) {
        state.websocket.send(JSON.stringify({
            action: "unsubscribe",
            account_ids: removed,
        }));
    }
    state.websocket.send(JSON.stringify({
        action: "subscribe",
        account_ids: accountIds,
    }));
    state.websocketSubscribedAccounts = new Set(accountIds);
}

function applyWebSocketSnapshot(payload) {
    const snapshot = (payload.accounts || []).find(
        (item) => item.account?.account_id === state.accountId,
    );
    if (!snapshot) return;
    state.account = snapshot.account;
    state.pnl = snapshot.pnl;
    state.positions = snapshot.positions || [];
    state.orders = snapshot.active_orders || [];
    state.trades = snapshot.today_trades || [];
    renderAccount(state.account, state.pnl);
    renderPositions(state.positions);
    renderOrders(state.orders);
    renderTrades(state.trades);
    elements.lastRefresh.textContent = `推送 ${formatTime(payload.generated_at)}`;
}

function upsertById(rows, payload, idField) {
    const index = rows.findIndex((row) => row[idField] === payload[idField]);
    if (index >= 0) {
        rows[index] = {...rows[index], ...payload};
    } else {
        rows.unshift(payload);
    }
}

function applyRealtimeEvent(event) {
    if (event.event_type === "HEARTBEAT") {
        state.websocket?.send(JSON.stringify({action: "pong"}));
        return;
    }
    if (event.event_type === "AUTH_EXPIRED") {
        refreshAccessToken().then((ok) => ok && connectRealtimeSocket());
        return;
    }
    if (event.event_type === "RESYNC_REQUIRED") {
        closeRealtimeSocket(false);
        scheduleWebSocketReconnect();
        return;
    }
    if (event.event_type === "ERROR") {
        showToast(`实时推送：${event.payload?.message || "请求失败"}`, "error");
        return;
    }
    if (event.event_type === "SNAPSHOT") {
        applyWebSocketSnapshot(event.payload || {});
        return;
    }
    if (event.account_id !== state.accountId) return;
    if (["ORDER_CREATED", "ORDER_UPDATED", "ORDER_CANCELLED"].includes(event.event_type)) {
        upsertById(state.orders, event.payload, "order_id");
        state.orders = state.orders.filter((order) => ACTIVE_ORDER_STATUSES.has(order.status));
        renderOrders(state.orders);
        return;
    }
    if (event.event_type === "TRADE_CREATED") {
        upsertById(state.trades, event.payload, "trade_id");
        renderTrades(state.trades);
        return;
    }
    if (["ACCOUNT_FACT_UPDATED", "ACCOUNT_UPDATED"].includes(event.event_type)
        && state.account && state.pnl) {
        const values = event.payload || {};
        // PostgreSQL账户事实只拥有基础资金字段；升级前的ACCOUNT_UPDATED
        // 也按事实域处理，绝不让其中的旧估值覆盖state.pnl。
        const factFields = [
            "cash_balance",
            "used_margin",
            "frozen_margin",
            "frozen_cash",
            "frozen_commission",
            "used_commission",
            "realized_pnl",
            "daily_close_pnl",
            "daily_commission",
            "risk_state",
            "updated_at",
        ];
        const factPatch = {};
        factFields.forEach((field) => {
            if (values[field] !== undefined) factPatch[field] = values[field];
        });
        state.account = {...state.account, ...factPatch};
        renderAccount(state.account, state.pnl);
        return;
    }
    if (event.event_type === "ACCOUNT_PNL_UPDATED" && state.account && state.pnl) {
        const values = event.payload || {};
        // Redis实时PnL只更新派生估值字段，不能覆盖cash_balance、冻结资金、
        // 实际保证金或手续费等PostgreSQL基础事实。
        state.pnl = {
            ...state.pnl,
            ...(values.cumulative_unrealized_pnl !== undefined
                ? {unrealized_pnl: values.cumulative_unrealized_pnl}
                : values.unrealized_pnl !== undefined
                    ? {unrealized_pnl: values.unrealized_pnl}
                    : {}),
            ...(values.daily_position_pnl !== undefined
                ? {daily_position_pnl: values.daily_position_pnl} : {}),
            ...(values.daily_close_pnl !== undefined
                ? {daily_close_pnl: values.daily_close_pnl} : {}),
            ...(values.daily_commission !== undefined
                ? {daily_commission: values.daily_commission} : {}),
            ...(values.daily_pnl !== undefined ? {daily_pnl: values.daily_pnl} : {}),
            ...(values.equity !== undefined ? {equity: values.equity} : {}),
            ...(values.available_cash !== undefined
                ? {available_cash: values.available_cash} : {}),
            ...(values.risk_ratio !== undefined
                ? {risk_ratio: values.risk_ratio} : {}),
            ...(values.updated_at !== undefined
                ? {updated_at: values.updated_at} : {}),
            ...(values.realtime_snapshot_version !== undefined
                ? {realtime_snapshot_version: values.realtime_snapshot_version}
                : {}),
            data_source: "REDIS_REALTIME",
        };
        renderAccount(state.account, state.pnl);
        return;
    }
    if (event.event_type === "POSITION_UPDATED") {
        const values = event.payload || {};
        const row = state.positions.find(
            (item) => item.position.position_id === values.position_id,
        );
        if (row) {
            row.position = {...row.position, ...values};
        } else {
            state.positions.unshift({
                position: values,
                pnl: {
                    mark_price: null,
                    unrealized_pnl: values.unrealized_pnl || "0",
                    daily_position_pnl: values.daily_position_pnl || "0",
                    data_source: "POSTGRES_FACT",
                },
                valuation: {},
                details: [],
            });
        }
        renderPositions(state.positions);
        return;
    }
    if (event.event_type === "POSITION_CLOSED") {
        state.positions = state.positions.filter(
            (item) => item.position.position_id !== event.payload?.position_id,
        );
        renderPositions(state.positions);
        return;
    }
    if (["PNL_UPDATED", "OPTION_VALUATION_UPDATED"].includes(event.event_type)) {
        const row = state.positions.find(
            (item) => item.position.position_id === event.entity_id,
        );
        if (!row) {
            return;
        }
        const values = event.payload || {};
        row.pnl = {
            ...row.pnl,
            mark_price: values.mark_price,
            unrealized_pnl: values.cumulative_unrealized_pnl,
            daily_position_pnl: values.daily_position_pnl,
            event_time: values.event_time,
            updated_at: values.updated_at,
            data_source: "REDIS_REALTIME",
        };
        renderPositions(state.positions);
    }
}

function scheduleWebSocketReconnect() {
    if (state.websocketManualClose || !state.accessToken) return;
    window.clearTimeout(state.websocketReconnectTimer);
    const delay = Math.min(1000 * (2 ** state.websocketReconnectAttempt), 30000);
    state.websocketReconnectAttempt += 1;
    setConnection(false, `实时推送重连中（${Math.round(delay / 1000)}秒）`);
    state.websocketReconnectTimer = window.setTimeout(connectRealtimeSocket, delay);
}

async function connectRealtimeSocket() {
    if (!state.accessToken || !state.accountId) return;
    closeRealtimeSocket(false);
    state.websocketManualClose = false;
    setConnection(false, "实时推送连接中");
    try {
        const result = await apiFetch("/api/ws/ticket", {method: "POST"});
        const socket = new WebSocket(websocketUrl(result.ticket));
        state.websocket = socket;
        socket.addEventListener("open", () => {
            state.websocketConnected = true;
            state.websocketReconnectAttempt = 0;
            setConnection(true, "WebSocket实时推送已连接");
            subscribeCurrentAccount();
            restartTimers();
        });
        socket.addEventListener("message", (message) => {
            try {
                applyRealtimeEvent(JSON.parse(message.data));
            } catch (_error) {
                showToast("收到无法解析的实时事件", "error");
            }
        });
        socket.addEventListener("close", () => {
            if (state.websocket !== socket) return;
            state.websocketConnected = false;
            state.websocket = null;
            restartTimers();
            scheduleWebSocketReconnect();
        });
        socket.addEventListener("error", () => {
            setConnection(false, "WebSocket实时推送连接失败");
        });
    } catch (error) {
        setConnection(false, `实时推送失败：${error.message}`);
        scheduleWebSocketReconnect();
    }
}

function setConnection(isOnline, text) {
    elements.connectionDot.className = `status-dot ${isOnline ? "online" : "offline"}`;
    elements.connectionText.textContent = text;
}

function showToast(message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    elements.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4500);
}

function generateClientOrderId() {
    const now = Date.now();
    const random = Math.random().toString(16).slice(2, 6).toUpperCase();
    elements.clientOrderId.value = `WEB-${now}-${random}`;
}

function emptyRow(columns, message) {
    return `<tr class="empty-row"><td colspan="${columns}">${escapeHtml(message)}</td></tr>`;
}

function renderAccount(account, pnl) {
    const cumulativeNetPnl = (
        asNumber(account.realized_pnl)
        + asNumber(pnl.unrealized_pnl)
        - asNumber(account.used_commission)
    );

    setMetric(elements.metricEquity, pnl.equity);
    setMetric(elements.metricAvailable, pnl.available_cash);
    setMetric(elements.metricNetPnl, cumulativeNetPnl);
    setMetric(elements.metricUnrealized, pnl.unrealized_pnl);
    setMetric(elements.metricRealized, account.realized_pnl);
    setMetric(elements.metricDailyPnl, pnl.daily_pnl);
    setMetric(elements.metricDailyPosition, pnl.daily_position_pnl);
    setMetric(elements.metricDailyClose, pnl.daily_close_pnl);
    elements.metricDailyCommission.textContent = formatMoney(pnl.daily_commission);
    elements.metricRisk.textContent = `${formatMoney(asNumber(pnl.risk_ratio) * 100)}%`;
    elements.metricSource.textContent =
        `${pnl.data_source} · ${formatTime(pnl.updated_at)}`;
    elements.metricAccountStatus.textContent =
        `账户状态 ${account.status}`;

    elements.accountName.textContent =
        `${account.account_name} · ${account.account_id}`;
    elements.accountTradingDay.textContent =
        `交易日 ${account.trading_day || "--"}`;
    elements.detailInitialCash.textContent = formatMoney(account.initial_cash);
    elements.detailCashBalance.textContent = formatMoney(account.cash_balance);
    elements.detailUsedMargin.textContent = formatMoney(account.used_margin);
    elements.detailFrozenMargin.textContent = formatMoney(account.frozen_margin);
    elements.detailFrozenCash.textContent = formatMoney(account.frozen_cash);
    elements.detailUsedCommission.textContent = formatMoney(account.used_commission);
    elements.detailFrozenCommission.textContent =
        formatMoney(account.frozen_commission);
    elements.detailAccountType.textContent =
        `${account.account_type} / ${account.status}`;
}

function renderPositions(positions) {
    elements.positionCount.textContent = `${positions.length} 条持仓`;
    if (!positions.length) {
        elements.positionsBody.innerHTML = emptyRow(14, "当前账户暂无持仓");
        return;
    }

    elements.positionsBody.innerHTML = positions.map(({position, pnl}) => {
        const directionClass = position.direction === "LONG" ? "buy" : "sell";
        const closeDirection = position.direction === "LONG" ? "SELL" : "BUY";
        return `
            <tr>
                <td>
                    <strong>${escapeHtml(position.symbol)}</strong>
                    <small>${escapeHtml(position.exchange_id)}</small>
                </td>
                <td><span class="direction-tag ${directionClass}">
                    ${escapeHtml(position.direction)}
                </span></td>
                <td class="number">
                    ${position.total_volume}/${position.today_volume}/${position.yesterday_volume}
                </td>
                <td class="number">
                    ${position.available_volume}/${position.frozen_volume}
                </td>
                <td class="number">${formatPrice(position.average_open_price)}</td>
                <td class="number">${formatPrice(pnl.mark_price)}</td>
                <td class="number ${pnlClass(pnl.unrealized_pnl)}">
                    ${formatMoney(pnl.unrealized_pnl)}
                </td>
                <td class="number ${pnlClass(pnl.daily_position_pnl)}">
                    ${formatMoney(pnl.daily_position_pnl)}
                </td>
                <td class="number ${pnlClass(position.realized_pnl)}">
                    ${formatMoney(position.realized_pnl)}
                </td>
                <td class="number ${pnlClass(position.daily_close_pnl)}">
                    ${formatMoney(position.daily_close_pnl)}
                </td>
                <td class="number">${formatMoney(position.used_margin)}</td>
                <td>${escapeHtml(position.trading_day)}</td>
                <td><span class="source-tag">${escapeHtml(pnl.data_source)}</span></td>
                <td>
                    <button class="table-action close-position" type="button"
                        data-symbol="${escapeHtml(position.symbol)}"
                        data-exchange="${escapeHtml(position.exchange_id)}"
                        data-direction="${closeDirection}"
                        data-volume="${position.available_volume}"
                        data-price="${escapeHtml(pnl.mark_price ?? "")}">
                        预填平仓
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

function orderStatusClass(status) {
    if (["ACCEPTED", "PARTIALLY_FILLED"].includes(status)) return "active";
    if (status === "FILLED") return "filled";
    return "cancelled";
}

function renderOrders(orders) {
    elements.orderCount.textContent = String(orders.length);
    if (!orders.length) {
        elements.ordersBody.innerHTML = emptyRow(12, "当前账户暂无订单");
        return;
    }

    // 新分页接口已经按最新记录在前返回，不再由页面二次反转。
    const rows = orders;
    elements.ordersBody.innerHTML = rows.map((order) => `
        <tr>
            <td title="${escapeHtml(order.order_id)}">
                <strong>${escapeHtml(order.order_id.slice(-12))}</strong>
                <small title="${escapeHtml(order.client_order_id)}">
                    ${escapeHtml(order.client_order_id)}
                </small>
            </td>
            <td><strong>${escapeHtml(order.symbol)}</strong></td>
            <td>
                <span class="${order.direction === "BUY" ? "buy" : "sell"}">
                    ${escapeHtml(order.direction)}
                </span>
                / ${escapeHtml(order.offset_flag)}
            </td>
            <td class="number">
                ${formatPrice(order.limit_price)}/${formatPrice(order.average_price)}
            </td>
            <td class="number">
                ${order.total_volume}/${order.traded_volume}/${order.remaining_volume}/${order.cancelled_volume}
            </td>
            <td class="number">${formatMoney(order.frozen_margin)}</td>
            <td class="number">${formatMoney(order.frozen_commission)}</td>
            <td class="number">${order.frozen_position_volume}</td>
            <td>
                <span class="status-tag ${orderStatusClass(order.status)}">
                    ${escapeHtml(order.status)}
                </span>
                <small>${escapeHtml(order.submit_status)}</small>
            </td>
            <td>${escapeHtml(order.trading_day)}</td>
            <td>${formatTime(order.updated_at, true)}</td>
            <td>
                ${ACTIVE_ORDER_STATUSES.has(order.status)
                    ? `<button class="table-action danger cancel-order"
                         type="button" data-order-id="${escapeHtml(order.order_id)}">
                         撤单
                       </button>`
                    : "--"}
            </td>
        </tr>
    `).join("");
}

function renderTrades(trades) {
    elements.tradeCount.textContent = String(trades.length);
    if (!trades.length) {
        elements.tradesBody.innerHTML = emptyRow(14, "当前账户暂无成交");
        return;
    }

    const rows = trades;
    elements.tradesBody.innerHTML = rows.map((trade) => `
        <tr>
            <td title="${escapeHtml(trade.trade_id)}">
                ${escapeHtml(trade.trade_id.slice(-12))}
            </td>
            <td title="${escapeHtml(trade.order_id)}">
                ${escapeHtml(trade.order_id.slice(-12))}
            </td>
            <td><strong>${escapeHtml(trade.symbol)}</strong></td>
            <td>
                <span class="${trade.direction === "BUY" ? "buy" : "sell"}">
                    ${escapeHtml(trade.direction)}
                </span>
                / ${escapeHtml(trade.offset_flag)}
            </td>
            <td class="number">${formatPrice(trade.trade_price)}</td>
            <td class="number">${trade.trade_volume}</td>
            <td class="number">${formatMoney(trade.turnover)}</td>
            <td class="number">${formatMoney(trade.margin)}</td>
            <td class="number">${formatMoney(trade.commission)}</td>
            <td class="number ${pnlClass(trade.realized_pnl)}">
                ${formatMoney(trade.realized_pnl)}
            </td>
            <td class="number ${pnlClass(trade.daily_close_pnl)}">
                ${formatMoney(trade.daily_close_pnl)}
            </td>
            <td>${escapeHtml(trade.trading_day)}</td>
            <td>${formatTime(trade.trade_time, true)}</td>
            <td>
                ${trade.offset_flag !== "OPEN"
                    ? `<button class="table-action trade-detail" type="button"
                         data-trade-id="${escapeHtml(trade.trade_id)}">
                         查看平仓明细
                       </button>`
                    : "--"}
            </td>
        </tr>
    `).join("");
}

async function showTradeAllocations(tradeId, button) {
    button.disabled = true;
    try {
        const allocations = await apiFetch(
            `/api/trades/${encodeURIComponent(tradeId)}/position-allocations`,
        );
        elements.tradeDetailTitle.textContent = `平仓成交明细 · ${tradeId}`;
        elements.tradeDetailBody.innerHTML = allocations.length
            ? allocations.map((item) => `
                <tr>
                    <td title="${escapeHtml(item.position_detail_id)}">
                        ${escapeHtml(item.position_detail_id.slice(-12))}
                    </td>
                    <td>${escapeHtml(item.resolved_offset_flag)}</td>
                    <td>${escapeHtml(item.open_trading_day)}</td>
                    <td class="number">${formatPrice(item.open_price)}</td>
                    <td class="number">${formatPrice(item.close_price)}</td>
                    <td class="number">${item.close_volume}</td>
                    <td class="number">${formatMoney(item.released_margin)}</td>
                    <td class="number">${formatMoney(item.commission)}</td>
                    <td class="number ${pnlClass(item.realized_pnl)}">
                        ${formatMoney(item.realized_pnl)}
                    </td>
                    <td class="number ${pnlClass(item.daily_close_pnl)}">
                        ${formatMoney(item.daily_close_pnl)}
                    </td>
                </tr>
            `).join("")
            : emptyRow(10, "该平仓成交暂无持仓分配明细");
        elements.tradeDetailDialog.showModal();
    } catch (error) {
        showToast(`读取平仓明细失败：${error.message}`, "error");
    } finally {
        button.disabled = false;
    }
}

async function refreshRealtime() {
    if (state.refreshing || !state.accountId) return;
    state.refreshing = true;

    try {
        const accountPath = encodeURIComponent(state.accountId);
        const snapshot = await apiFetch(
            `/api/accounts/${accountPath}/trading-snapshot`,
        );
        const positionPnl = snapshot.positions;

        state.positions = positionPnl;
        state.account = snapshot.account;
        state.pnl = snapshot.pnl;
        renderAccount(snapshot.account, snapshot.pnl);
        renderPositions(positionPnl);
        setConnection(true, "后端与实时快照已连接");
        elements.lastRefresh.textContent = `刷新 ${formatTime(new Date().toISOString())}`;
    } catch (error) {
        setConnection(false, error.message);
        elements.lastRefresh.textContent = "刷新失败";
    } finally {
        state.refreshing = false;
    }
}

async function refreshOrdersAndTrades() {
    if (!state.accountId) return;
    try {
        const accountPath = encodeURIComponent(state.accountId);
        const [orderPage, tradePage] = await Promise.all([
            apiFetch(`/api/orders/page?account_id=${accountPath}&limit=100`),
            apiFetch(`/api/trades/page?account_id=${accountPath}&limit=100`),
        ]);
        state.orders = orderPage.items;
        state.trades = tradePage.items;
        renderOrders(state.orders);
        renderTrades(state.trades);
    } catch (error) {
        setConnection(false, error.message);
    }
}

function restartTimers() {
    window.clearInterval(state.refreshTimer);
    window.clearInterval(state.slowRefreshTimer);
    state.refreshTimer = null;
    state.slowRefreshTimer = null;

    // WebSocket在线时只使用推送；断线期间保留原HTTP轮询作为开发联调兜底。
    if (elements.autoRefresh.checked && !state.websocketConnected) {
        state.refreshTimer = window.setInterval(refreshRealtime, REFRESH_INTERVAL_MS);
        state.slowRefreshTimer = window.setInterval(
            refreshOrdersAndTrades,
            SLOW_REFRESH_INTERVAL_MS,
        );
    }
}

async function loadAccount() {
    const accountId = elements.accountId.value.trim();
    if (!accountId) {
        showToast("请输入账户编号", "error");
        return;
    }
    state.accountId = accountId;
    elements.orderAccountId.value = accountId;
    await Promise.all([refreshRealtime(), refreshOrdersAndTrades()]);
    subscribeCurrentAccount();
    restartTimers();
}

async function submitOrder(event) {
    event.preventDefault();
    elements.submitOrder.disabled = true;
    elements.submitOrder.textContent = "正在提交…";

    const formData = new FormData(elements.orderForm);
    const payload = {
        client_order_id: String(formData.get("client_order_id")).trim(),
        account_id: String(formData.get("account_id")).trim(),
        exchange_id: String(formData.get("exchange_id")).trim().toUpperCase(),
        symbol: String(formData.get("symbol")).trim().toUpperCase(),
        direction: formData.get("direction"),
        offset_flag: formData.get("offset_flag"),
        order_type: "LIMIT",
        limit_price: String(formData.get("limit_price")).trim(),
        volume: Number(formData.get("volume")),
    };

    try {
        const order = await apiFetch("/api/orders", {
            method: "POST",
            body: JSON.stringify(payload),
        });
        showToast(`订单已接收：${order.order_id}（${order.status}）`);
        generateClientOrderId();
        if (payload.account_id !== state.accountId) {
            elements.accountId.value = payload.account_id;
            state.accountId = payload.account_id;
        }
        await Promise.all([refreshRealtime(), refreshOrdersAndTrades()]);
    } catch (error) {
        showToast(`下单失败：${error.message}`, "error");
    } finally {
        elements.submitOrder.disabled = false;
        elements.submitOrder.textContent = "提交限价订单";
    }
}

async function cancelOrder(orderId, button) {
    if (!window.confirm(`确定撤销订单 ${orderId} 的剩余数量吗？`)) return;
    button.disabled = true;
    try {
        const order = await apiFetch(
            `/api/orders/${encodeURIComponent(orderId)}/cancel`,
            {
                method: "POST",
                body: JSON.stringify({account_id: state.accountId}),
            },
        );
        showToast(`撤单完成：${order.status}`);
        await Promise.all([refreshRealtime(), refreshOrdersAndTrades()]);
    } catch (error) {
        showToast(`撤单失败：${error.message}`, "error");
        button.disabled = false;
    }
}

function prefillCloseOrder(button) {
    elements.exchangeId.value = button.dataset.exchange;
    elements.symbol.value = button.dataset.symbol;
    elements.offsetFlag.value = "CLOSE";
    elements.volume.value = Math.max(Number(button.dataset.volume) || 1, 1);
    if (button.dataset.price) elements.limitPrice.value = button.dataset.price;
    const direction = button.dataset.direction;
    const radio = elements.orderForm.querySelector(
        `input[name="direction"][value="${direction}"]`,
    );
    if (radio) radio.checked = true;
    generateClientOrderId();
    elements.limitPrice.focus();
    elements.orderForm.scrollIntoView({behavior: "smooth", block: "center"});
    showToast(`已预填 ${button.dataset.symbol} 平仓单，请确认价格和数量后提交`);
}

document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((tab) => {
            tab.classList.toggle("active", tab === button);
        });
        document.querySelector("#orders-panel").classList.toggle(
            "hidden",
            button.dataset.tab !== "orders",
        );
        document.querySelector("#trades-panel").classList.toggle(
            "hidden",
            button.dataset.tab !== "trades",
        );
    });
});

elements.positionsBody.addEventListener("click", (event) => {
    const button = event.target.closest(".close-position");
    if (button) prefillCloseOrder(button);
});

elements.ordersBody.addEventListener("click", (event) => {
    const button = event.target.closest(".cancel-order");
    if (button) cancelOrder(button.dataset.orderId, button);
});

elements.tradesBody.addEventListener("click", (event) => {
    const button = event.target.closest(".trade-detail");
    if (button) showTradeAllocations(button.dataset.tradeId, button);
});

elements.closeTradeDialog.addEventListener("click", () => {
    elements.tradeDetailDialog.close();
});
elements.tradeDetailDialog.addEventListener("click", (event) => {
    if (event.target === elements.tradeDetailDialog) {
        elements.tradeDetailDialog.close();
    }
});

elements.loadAccount.addEventListener("click", loadAccount);
elements.refreshNow.addEventListener("click", async () => {
    await Promise.all([refreshRealtime(), refreshOrdersAndTrades()]);
});
elements.autoRefresh.addEventListener("change", restartTimers);
elements.orderForm.addEventListener("submit", submitOrder);
elements.regenerateId.addEventListener("click", generateClientOrderId);
elements.accountId.addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadAccount();
});

elements.loginForm.addEventListener("submit", login);
elements.logoutButton.addEventListener("click", logout);
generateClientOrderId();
refreshAccessToken()
    .then((restored) => restored ? loadAuthorizedAccounts() : showLogin())
    .catch(showLogin);
