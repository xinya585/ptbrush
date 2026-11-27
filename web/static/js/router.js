import { createRouter, createWebHistory } from 'vue-router';
import Dashboard from './views/Dashboard.js';
import Sites from './views/Sites.js';
import Downloader from './views/Downloader.js';
import Config from './views/Config.js';
import Strategy from './views/Strategy.js';
import QuickStart from './views/QuickStart.js';
import Search from './views/Search.js';
import Logs from './views/Logs.js';
import Security from './views/Security.js';
import Verified from './views/Verified.js';
import Login from './views/Login.js';

export const router = createRouter({
    history: createWebHistory(),
    routes: [
        { path: '/', redirect: '/dashboard' },
        { path: '/dashboard', component: Dashboard, meta: { title: '仪表盘' } },
        { path: '/sites', component: Sites, meta: { title: '站点配置' } },
        { path: '/downloader', component: Downloader, meta: { title: '下载器配置' } },
        { path: '/brush', component: Config, meta: { title: '刷流配置' } },
        { path: '/strategy', component: Strategy, meta: { title: '策略中心' } },
        { path: '/start', component: QuickStart, meta: { title: '快速开始' } },
        { path: '/search', component: Search, meta: { title: '搜片' } },
        { path: '/logs', component: Logs, meta: { title: '日志' } },
        { path: '/security', component: Security, meta: { title: '系统设置' } },
        { path: '/verified', component: Verified, meta: { title: '详单' } },
        // 老链接和书签还指向 /config，统一落到刷流配置
        { path: '/config', redirect: '/brush' },
        { path: '/login', component: Login, meta: { title: '登录', public: true } },
    ],
});

router.afterEach((to) => {
    if (to.meta?.title) document.title = `${to.meta.title} - PTBrush`;
});

let authStatePromise = null;
function fetchAuthState() {
    if (!authStatePromise) {
        authStatePromise = fetch('/api/auth/state', { credentials: 'same-origin' })
            .then(r => r.ok ? r.json() : { login_required: false, authenticated: true })
            .catch(() => ({ login_required: false, authenticated: true }));
    }
    return authStatePromise;
}

export function refreshAuthState() {
    authStatePromise = null;
}

router.beforeEach(async (to) => {
    if (to.meta?.public) return true;
    const state = await fetchAuthState();
    if (state.login_required && !state.authenticated) {
        return { path: '/login', query: { from: to.fullPath } };
    }
    return true;
});
