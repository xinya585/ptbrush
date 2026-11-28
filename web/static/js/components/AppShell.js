import { defineComponent, ref, onMounted, onUnmounted, nextTick, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';
import { refreshAuthState } from '../router.js';

export default defineComponent({
    name: 'AppShell',
    setup() {
        const authState = ref({ login_required: false, authenticated: true });

        async function loadAuthState() {
            try {
                authState.value = await api.get('/api/auth/state');
            } catch (e) {
                // ignore
            }
        }

        async function logout() {
            try {
                await api.post('/api/auth/logout');
                refreshAuthState();
                ElMessage.success('已退出登录');
                setTimeout(() => window.location.replace('/login'), 300);
            } catch (e) {
                ElMessage.error(e.message || '退出失败');
            }
        }

        const version = ref('');

        async function loadVersion() {
            try {
                const data = await api.get('/api/version');
                version.value = data.version;
            } catch (e) {
                // ignore
            }
        }

        // 内容区滚动条只占 .el-main 的宽度，header 没有，
        // 不补偿的话顶部会比页面内容右移半个滚动条宽度（约 7~8px）。
        const headerPadRight = ref(0);

        function syncScrollbar() {
            if (isMobile.value) {
                headerPadRight.value = 0;
                return;
            }
            const main = document.querySelector('.el-main');
            if (!main) return;
            const sb = main.offsetWidth - main.clientWidth;
            headerPadRight.value = sb > 0 ? sb : 0;
        }

        // 窄屏（手机 / 小屏平板）顶部横向菜单放不下，改成汉堡 + 右侧抽屉。
        const isMobile = ref(false);
        const menuOpen = ref(false);
        let mq = null;

        function applyViewport(e) {
            isMobile.value = !!e.matches;
            if (!isMobile.value) menuOpen.value = false;
            nextTick(syncScrollbar);
        }

        watch(menuOpen, () => { /* 抽屉开合后重新测滚动条 */ nextTick(syncScrollbar); });

        onMounted(() => {
            loadAuthState();
            loadVersion();
            mq = window.matchMedia('(max-width: 820px)');
            applyViewport(mq);
            if (mq.addEventListener) mq.addEventListener('change', applyViewport);
            // 首次渲染时内容还没撑出滚动条，必须等数据加载后重新测；
            // 之后内容增加/减少会改变 .el-main 的 content 宽度，用 ResizeObserver 跟进。
            nextTick(() => {
                syncScrollbar();
                const main = document.querySelector('.el-main');
                if (main && window.ResizeObserver) {
                    new ResizeObserver(syncScrollbar).observe(main);
                }
                setTimeout(syncScrollbar, 800);
            });
            window.addEventListener('resize', syncScrollbar);
        });

        onUnmounted(() => {
            window.removeEventListener('resize', syncScrollbar);
            if (mq && mq.removeEventListener) mq.removeEventListener('change', applyViewport);
        });

        return { authState, logout, version, headerPadRight, isMobile, menuOpen };
    },
    template: `
        <el-container style="height:100vh;height:100dvh;">
            <el-header class="app-header" :height="isMobile ? '52px' : '56px'" :style="{ paddingRight: headerPadRight + 'px' }">
                <div class="app-header-inner">
                <div class="app-brand">
                    <img class="app-logo" src="/static/img/logo-ptbrush.png?v=1.0.0.20261115z2" alt="PTBrush" />
                    <span class="app-version" v-if="version">v{{ version }}</span>
                </div>
                <el-menu
                    v-if="!isMobile"
                    mode="horizontal"
                    :ellipsis="false"
                    :default-active="$route.path"
                    background-color="#1f2937"
                    text-color="#cbd5e1"
                    active-text-color="#67c23a"
                    router>
                    <el-menu-item index="/dashboard">仪表盘</el-menu-item>
                    <el-menu-item index="/sites">站点配置</el-menu-item>
                    <el-menu-item index="/downloader">下载器配置</el-menu-item>
                    <el-menu-item index="/brush">刷流配置</el-menu-item>
                    <el-menu-item index="/start">快速开始</el-menu-item>
                    <el-menu-item index="/search">搜片</el-menu-item>
                    <el-menu-item index="/logs">日志</el-menu-item>
                    <el-menu-item index="/security">系统设置</el-menu-item>
                </el-menu>
                <div v-if="isMobile" class="app-header-mobile">
                    <button class="app-nav-toggle" type="button" aria-label="打开导航" @click="menuOpen = true">
                        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                            <line x1="3" y1="6" x2="21" y2="6" />
                            <line x1="3" y1="12" x2="21" y2="12" />
                            <line x1="3" y1="18" x2="21" y2="18" />
                        </svg>
                    </button>
                </div>
                <el-button
                    v-if="authState.login_required && authState.authenticated"
                    type="info" plain size="small" @click="logout">
                    退出
                </el-button>
                </div>
            </el-header>
            <el-main>
                <router-view />
            </el-main>
            <el-footer class="app-footer" :height="isMobile ? '30px' : '34px'">
                <span v-if="version">PTBrush v{{ version }}</span>
            </el-footer>

            <el-drawer
                v-if="isMobile"
                v-model="menuOpen"
                direction="rtl"
                size="248px"
                title="导航"
                class="app-nav-drawer">
                <el-menu
                    mode="vertical"
                    :default-active="$route.path"
                    background-color="#1f2937"
                    text-color="#cbd5e1"
                    active-text-color="#67c23a"
                    router
                    @select="menuOpen = false">
                    <el-menu-item index="/dashboard">仪表盘</el-menu-item>
                    <el-menu-item index="/sites">站点配置</el-menu-item>
                    <el-menu-item index="/downloader">下载器配置</el-menu-item>
                    <el-menu-item index="/brush">刷流配置</el-menu-item>
                    <el-menu-item index="/start">快速开始</el-menu-item>
                    <el-menu-item index="/search">搜片</el-menu-item>
                    <el-menu-item index="/logs">日志</el-menu-item>
                    <el-menu-item index="/security">系统设置</el-menu-item>
                </el-menu>
            </el-drawer>
        </el-container>
    `,
});
