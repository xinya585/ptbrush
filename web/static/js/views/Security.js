import { defineComponent, ref, onMounted } from 'vue';
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
                // 备份包里带着 [update] 段，导入后节点选择可能变了，顺手同步一下界面（容器版没有这块）
                if (portable.value) await loadNodes(false);
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
        // 只有 Windows 便携版能自己下载 zip 替换升级；容器版把整块收成一条升级说明
        const portable = ref(true);

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

        onMounted(async () => {
            load();
            loadBackup();
            try {
                const info = await api.get('/api/version');
                // 老版本后端没有 portable 字段，按便携版处理，保持原行为
                portable.value = info.portable !== false;
            } catch (e) {
                // ignore
            }
            if (portable.value) loadNodes(false);
        });

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

            <el-card shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>{{ portable ? '下载与更新节点' : '程序升级' }}</strong>
                        <div v-if="portable" class="node-actions">
                            <el-button size="small" plain :loading="nodeProbing" @click="probeNodes">测速</el-button>
                        </div>
                    </div>
                </template>
                <div v-if="portable" class="node-block">
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
                <div v-else class="node-alt">
                    <div class="node-alt-title">当前是容器部署，升级走镜像，不走这个下载节点</div>
                    <div class="node-alt-desc">
                        程序内置的「下载安装包 → 替换文件 → 自动重启」只对 Windows 便携版有效。容器里代码在镜像内，替换不了自己，所以这组节点选择在容器版没有作用，已经收起来了。
                    </div>
                    <div class="node-alt-code">docker pull xinya585/ptbrush:latest</div>
                    <div class="node-alt-code">docker compose up -d</div>
                    <div class="node-alt-desc">
                        检查更新在容器里也不会自动跑；想看有没有新版本，比一下本页顶部版本号和官网 <a href="https://zta.cc.cd/changelog.html" target="_blank" rel="noopener">更新日志</a> 即可。三条下载线路是为 Windows 便携版准备的下包镜像。
                    </div>
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
