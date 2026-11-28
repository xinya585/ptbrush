import { defineComponent, ref, computed, onMounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api.js';
import PasswordCard from '../components/PasswordCard.js';
import VerifiedPanel from '../components/VerifiedPanel.js';

/**
 * 系统设置页。
 *
 * 三块内容：
 * 1. Web 访问        —— 登录密码（PasswordCard 组件）
 * 2. 备份与恢复      —— 一键导出全部配置数据；换设备导入恢复
 * 3. 下载与更新节点  —— 安装包走哪个节点（CF / 海外），更新清单一律自动选最快
 *
 * CookieCloud 同步已迁到「站点配置」页；详单作为本页「详单」页签。
 */

export default defineComponent({
    name: 'Security',
    components: { PasswordCard, VerifiedPanel },
    setup() {
        const loading = ref(false);
        const loginRequired = ref(false);
        const tab = ref('settings');

        async function load() {
            loading.value = true;
            try {
                const res = await api.get('/api/security');
                loginRequired.value = !!res?.web?.login_required;
            } catch (e) {
                ElMessage.error(e.message || '加载失败');
            } finally {
                loading.value = false;
            }
        }

        // ---- 备份与恢复 ----
        const exporting = ref(false);
        const importing = ref(false);
        const importDone = ref(null);
        const backupInfo = ref(null);

        async function loadBackup() {
            try {
                backupInfo.value = await api.get('/api/backup/info');
            } catch (e) {
                // 备份信息只用于展示，失败不打扰用户
            }
        }

        async function exportBackup() {
            exporting.value = true;
            try {
                // 用 fetch + blob：失败时能把后端的错误文案留在页面上，而不是把标签页跳到一段 JSON
                const res = await fetch('/api/backup/export', { credentials: 'same-origin' });
                if (res.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.error || `导出失败 (${res.status})`);
                }
                const blob = await res.blob();
                const cd = res.headers.get('Content-Disposition') || '';
                const hit = /filename="?([^";]+)"?/i.exec(cd);
                const name = (hit && hit[1]) || `ptbrush-backup-${Date.now()}.zip`;
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = name;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(url), 2000);
                ElMessage.success(`备份已导出：${name}（${(blob.size / 1048576).toFixed(1)} MB）`);
                await loadBackup();
            } catch (e) {
                ElMessage.error(e.message || '导出失败');
            } finally {
                exporting.value = false;
            }
        }

        async function pickBackup(uploadFile) {
            const raw = uploadFile && uploadFile.raw;
            if (!raw) return;
            if (!/\.zip$/i.test(raw.name || '')) {
                ElMessage.warning('请选择 .zip 备份包');
                return;
            }
            importing.value = true;
            try {
                const fd = new FormData();
                fd.append('file', raw, raw.name);
                const res = await fetch('/api/backup/import', {
                    method: 'POST',
                    body: fd,
                    credentials: 'same-origin',
                });
                const data = await res.json().catch(() => ({}));
                if (res.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                if (!res.ok) throw new Error(data.error || `导入失败 (${res.status})`);
                importDone.value = data;
                ElMessage.success(data.message || '导入完成');
                await loadBackup();
                // 备份包里带着 [update] 段，导入后节点选择可能变了，顺手同步一下界面（只有便携版有这块）
                if (deploy.value === 'windows') await loadNodes(false);
            } catch (e) {
                ElMessage.error(e.message || '导入失败');
            } finally {
                importing.value = false;
            }
        }

        async function restartApp() {
            try {
                await ElMessageBox.confirm(
                    '导入后要重启 PTBrush，站点、下载器和数据库才会全部按新配置加载。现在重启吗？',
                    '重启生效',
                    { type: 'warning', confirmButtonText: '立即重启', cancelButtonText: '稍后手动重启' },
                );
            } catch (e) {
                return;
            }
            try {
                await api.post('/api/backup/restart');
                ElMessage.success('正在重启，约 5 秒后自动刷新页面');
                setTimeout(() => location.reload(), 5000);
            } catch (e) {
                ElMessage.error(e.message || '重启请求失败');
            }
        }

        // ---- 下载 / 更新节点 ----
        const nodeForm = ref('cf');
        const nodeRows = ref([]);
        const nodeSaving = ref(false);
        const nodeProbing = ref(false);
        // 部署形态：windows（便携版，保留节点选择） / linux（原生安装，网页内升级） / docker、source（不显示升级）
        const deploy = ref('windows');
        const currentVersion = ref('');

        async function loadNodes(probe) {
            try {
                const res = await api.get('/api/update/nodes' + (probe ? '?probe=1' : ''));
                nodeForm.value = res.current || 'cf';
                nodeRows.value = res.nodes || [];
            } catch (e) {
                if (probe) ElMessage.error(e.message || '测速失败');
            }
        }

        async function probeNodes() {
            nodeProbing.value = true;
            try {
                await loadNodes(true);
            } finally {
                nodeProbing.value = false;
            }
        }

        async function saveNode() {
            nodeSaving.value = true;
            try {
                const res = await api.post('/api/update/node', { node: nodeForm.value });
                ElMessage.success(res.message || '下载节点已保存');
                await loadNodes(false);
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                nodeSaving.value = false;
            }
        }

        // ---- Linux 程序升级 ----
        const upd = ref({ latest: null, has_update: false, notes: '', checked_at: null, error: null, size: null });
        const checking = ref(false);
        const upgrading = ref(false);

        const updStatus = computed(() => {
            if (checking.value) return '检查中…';
            if (upgrading.value) return '正在升级…';
            if (upd.value.error) return '检查失败';
            if (!upd.value.latest) return '还没有检查过';
            return upd.value.has_update ? `有新版本 ${upd.value.latest}` : '已是最新版本';
        });

        async function checkUpdate() {
            checking.value = true;
            try {
                upd.value = await api.post('/api/update/check');
                if (upd.value.error) ElMessage.error(upd.value.error);
                else if (upd.value.has_update) ElMessage.success(`发现新版本 ${upd.value.latest}`);
                else ElMessage.success('已是最新版本');
            } catch (e) {
                ElMessage.error(e.message || '检查更新失败');
            } finally {
                checking.value = false;
            }
        }

        async function upgradeNow() {
            try {
                await ElMessageBox.confirm(
                    `将下载 ${upd.value.latest} 的安装包，校验后替换程序文件并重启服务，约 10 秒不可用。站点、下载器配置都在数据目录里，不受影响。现在升级吗？`,
                    '升级并重启',
                    { type: 'warning', confirmButtonText: '立即升级', cancelButtonText: '取消' },
                );
            } catch (e) {
                return;
            }
            upgrading.value = true;
            try {
                const res = await api.post('/api/update/oneclick');
                ElMessage.success(res.message || '正在替换程序文件，稍后自动重启');
                setTimeout(() => location.reload(), 8000);
            } catch (e) {
                upgrading.value = false;
                ElMessage.error(e.message || '升级失败');
            }
        }

        onMounted(async () => {
            load();
            loadBackup();
            try {
                const info = await api.get('/api/version');
                // 老版本后端没有 deploy 字段，按便携版处理，保持原行为
                deploy.value = info.deploy || 'windows';
                currentVersion.value = info.version || '';
                if (info.version) upd.value.current = info.version;
            } catch (e) {
                // ignore
            }
            if (deploy.value === 'windows') loadNodes(false);
        });

        return {
            tab, loading, loginRequired, load,
            exporting, importing, importDone, backupInfo,
            exportBackup, pickBackup, restartApp,
            nodeForm, nodeRows, nodeSaving, nodeProbing, deploy,
            saveNode, probeNodes,
            currentVersion, upd, checking, upgrading, updStatus, checkUpdate, upgradeNow,
        };

        return {
            tab, loading, loginRequired, load,
            exporting, importing, importDone, backupInfo,
            exportBackup, pickBackup, restartApp,
            nodeForm, nodeRows, nodeSaving, nodeProbing, portable,
            saveNode, probeNodes,
        };
    },
    template: `
        <div class="sites-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">系统设置</div>
                </div>
            </div>

            <el-tabs v-model="tab" class="settings-tabs">
                <el-tab-pane label="常规设置" name="settings">
                    <div style="margin-bottom:16px;">
                        <PasswordCard :login-required="loginRequired" />
                    </div>

            <el-card shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>备份与恢复</strong>
                    </div>
                </template>
                <div class="backup-block">
                    <div class="backup-desc">
                        把站点配置、下载器、CookieCloud 凭据、账号数据与统计整体打包成一个 zip；换设备后在新的 PTBrush 里导入这个包，就能恢复成现在这台机器的状态。
                    </div>
                    <div class="backup-actions">
                        <el-button type="primary" :loading="exporting" @click="exportBackup">导出备份</el-button>
                        <el-upload
                            :show-file-list="false"
                            :auto-upload="false"
                            accept=".zip"
                            :on-change="pickBackup">
                            <el-button :loading="importing">导入备份</el-button>
                        </el-upload>
                        <el-button v-if="importDone" type="warning" plain @click="restartApp">重启生效</el-button>
                    </div>
                    <div v-if="backupInfo" class="backup-meta">
                        当前数据 {{ backupInfo.files.length }} 个文件（不含日志与升级包）
                        <template v-if="backupInfo.backups && backupInfo.backups.length">
                            <span class="backup-sep">·</span>
                            最近一次备份 {{ backupInfo.backups[0].time }}
                        </template>
                    </div>
                    <div v-if="importDone" class="backup-hint">
                        已导入 {{ (importDone.replaced || []).length }} 个文件<template v-if="importDone.auto_backup">，导入前的数据自动备份为 {{ importDone.auto_backup }}</template>。重启后全部生效。
                    </div>
                </div>
            </el-card>

            <el-card v-if="deploy === 'windows'" shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>下载与更新节点</strong>
                        <div class="node-actions">
                            <el-button size="small" plain :loading="nodeProbing" @click="probeNodes">测速</el-button>
                        </div>
                    </div>
                </template>
                <div class="node-block">
                    <div class="node-desc">
                        检查更新时两个节点并发拉版本清单，自动用最快的那一个；下载安装包按下面选的节点走，失败会自动换下一个节点继续。
                    </div>
                    <div class="node-grid">
                        <div
                            v-for="n in nodeRows"
                            :key="n.key"
                            class="node-card"
                            :class="{ 'is-current': n.key === nodeForm }"
                            @click="nodeForm = n.key"
                        >
                            <div class="node-card-head">
                                <span class="node-card-name">{{ n.label }}</span>
                                <span v-if="n.key === nodeForm" class="node-card-flag">已选</span>
                            </div>
                            <div class="node-card-lat" :class="{ 'is-bad': n.ok === false, 'is-idle': n.latency == null && n.ok == null }">
                                <template v-if="n.latency != null">{{ n.latency }}<span class="node-card-unit"> ms</span></template>
                                <template v-else-if="n.ok === false">{{ n.error || '不可用' }}</template>
                                <template v-else>未测速</template>
                            </div>
                            <div class="node-card-hint">{{ n.key === nodeForm ? '下载走这个节点' : '点击选用' }}</div>
                        </div>
                    </div>
                    <div class="node-save">
                        <el-button type="primary" :loading="nodeSaving" @click="saveNode">保存节点</el-button>
                        <span class="node-tip">改完立即生效，下次检查更新就用新节点。</span>
                    </div>
                </div>
            </el-card>

            <el-card v-else-if="deploy === 'linux'" shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>程序升级</strong>
                        <span class="upd-badge">Linux 原生安装</span>
                    </div>
                </template>
                <div class="node-block">
                    <div class="node-desc">
                        可以在网页里直接升级：检查更新 → 下载 tar.gz → 校验 SHA256 → 替换程序文件 → 服务自动重启。数据目录（默认 <code>/var/lib/ptbrush</code>）不动，配置、站点和数据库都保留。
                    </div>
                    <div class="upd-grid">
                        <div class="upd-item">
                            <span class="upd-label">当前版本</span>
                            <b>v{{ currentVersion || '—' }}</b>
                        </div>
                        <div class="upd-item">
                            <span class="upd-label">最新版本</span>
                            <b>{{ upd.latest ? 'v' + upd.latest : '未检查' }}</b>
                        </div>
                        <div class="upd-item">
                            <span class="upd-label">状态</span>
                            <b :class="{ 'upd-new': upd.has_update, 'upd-bad': !!upd.error }">{{ updStatus }}</b>
                        </div>
                        <div class="upd-item">
                            <span class="upd-label">安装包</span>
                            <b>{{ upd.size ? (upd.size / 1048576).toFixed(1) + ' MB' : '—' }}</b>
                        </div>
                    </div>
                    <div class="node-save">
                        <el-button :loading="checking" @click="checkUpdate">检查更新</el-button>
                        <el-button type="primary" :disabled="!upd.has_update || upgrading" :loading="upgrading" @click="upgradeNow">立即升级</el-button>
                        <span class="node-tip">升级时服务会重启几秒；升级包走 Cloudflare，失败自动改走 GitHub Release。</span>
                    </div>
                    <div v-if="upd.error" class="upd-error">{{ upd.error }}</div>
                    <div v-if="upd.has_update && upd.notes" class="upd-notes">{{ upd.notes }}</div>
                </div>
            </el-card>
                </el-tab-pane>

                <el-tab-pane label="详单" name="verified" lazy>
                    <VerifiedPanel />
                </el-tab-pane>
            </el-tabs>
        </div>
    `,
});
