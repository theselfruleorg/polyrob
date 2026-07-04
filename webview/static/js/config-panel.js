/**
 * Session Configuration Panel
 * Handles dynamic configuration for new task agent sessions
 */
import { logger, sessionState } from '/static/js/ui-utils.js?v=2';

class ConfigPanel {
    constructor() {
        this.capabilities = null;
        this.selectedModel = null;
        this.selectedTools = [];  // Regular tools (browser, filesystem, etc.)
        this.selectedMCPServers = [];  // MCP servers (mcp:anysite, mcp:ghost, etc.)
        this.maxSteps = 50;

        // DOM elements
        this.panel = document.getElementById('config-panel');
        this.modelSelect = document.getElementById('config-model');
        this.toolsGroup = document.getElementById('tools-group');
        this.maxStepsInput = document.getElementById('config-max-steps');
        this.maxStepsValue = document.getElementById('max-steps-value');
        this.advancedSection = document.getElementById('config-advanced');

        this.init();
    }

    async init() {
        logger.debug('[ConfigPanel] Initializing');

        // Show panel - remove hidden class (not just style.display due to !important)
        if (this.panel) {
            this.panel.classList.remove('hidden');
            this.panel.style.display = 'flex';
        }

        // Set up event listeners
        this.setupEventListeners();

        // Load capabilities
        await this.loadCapabilities();

        // Populate form
        this.populateForm();
    }

    setupEventListeners() {
        // Model selection
        if (this.modelSelect) {
            this.modelSelect.addEventListener('change', () => {
                this.selectedModel = this.modelSelect.value;
                this.updateModelInfo();
            });
        }

        // Advanced section toggle
        if (this.advancedSection) {
            const header = this.advancedSection.querySelector('.config-advanced-header');
            header.addEventListener('click', () => {
                this.advancedSection.classList.toggle('open');
            });
        }

        // Max steps slider
        if (this.maxStepsInput && this.maxStepsValue) {
            this.maxStepsInput.addEventListener('input', (e) => {
                this.maxSteps = parseInt(e.target.value);
                this.maxStepsValue.textContent = this.maxSteps;
            });
        }
    }

    async loadCapabilities() {
        try {
            const response = await fetch('/api/task/capabilities');
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            this.capabilities = await response.json();
            logger.debug('[ConfigPanel] Loaded capabilities:', this.capabilities);

        } catch (error) {
            logger.error('[ConfigPanel] Failed to load capabilities:', error);
            this.showError('Failed to load available models and tools');
        }
    }

    populateForm() {
        if (!this.capabilities) {
            logger.error('[ConfigPanel] No capabilities loaded');
            return;
        }

        // Populate model dropdown
        this.populateModels();

        // Populate tools checkboxes (includes MCP servers)
        this.populateTools();
    }

    populateModels() {
        if (!this.modelSelect || !this.capabilities.models) return;

        // Store model data for custom dropdown
        this.modelData = {};
        this.capabilities.models.forEach(model => {
            this.modelData[model.model] = model;
        });

        // Create custom dropdown structure
        this.createCustomDropdown();
    }

    createCustomDropdown() {
        // Replace native select with custom dropdown
        const selectContainer = this.modelSelect.parentElement;

        // Hide native select
        this.modelSelect.style.display = 'none';

        // Create custom dropdown
        const customDropdown = document.createElement('div');
        customDropdown.className = 'model-dropdown';
        customDropdown.id = 'custom-model-dropdown';

        // Create selected display
        const selectedDisplay = document.createElement('div');
        selectedDisplay.className = 'model-dropdown-selected';
        selectedDisplay.innerHTML = '<span class="model-dropdown-text">Select model...</span><span class="model-dropdown-arrow">▼</span>';

        // Create options container
        const optionsContainer = document.createElement('div');
        optionsContainer.className = 'model-dropdown-options';

        // Group models by provider
        const modelsByProvider = {};
        this.capabilities.models.forEach(model => {
            if (!modelsByProvider[model.provider]) {
                modelsByProvider[model.provider] = [];
            }
            modelsByProvider[model.provider].push(model);
        });

        let firstModel = null;
        let matchFound = false;

        // Create option groups
        Object.entries(modelsByProvider).forEach(([provider, models]) => {
            // Provider group header
            const groupHeader = document.createElement('div');
            groupHeader.className = 'model-dropdown-group';
            groupHeader.textContent = provider.charAt(0).toUpperCase() + provider.slice(1);
            optionsContainer.appendChild(groupHeader);

            // Model options
            models.forEach(model => {
                if (!firstModel) firstModel = model.model;

                const option = document.createElement('div');
                option.className = 'model-dropdown-option';
                option.dataset.value = model.model;

                // Model name
                const nameSpan = document.createElement('span');
                nameSpan.className = 'model-option-name';
                nameSpan.textContent = model.model;

                // Indicators container
                const indicators = document.createElement('span');
                indicators.className = 'model-option-indicators';

                // Price indicator (based on output price per 1M tokens)
                if (model.price_output !== undefined) {
                    const priceIndicator = document.createElement('span');
                    priceIndicator.className = 'model-indicator model-indicator-price';
                    priceIndicator.textContent = this.getPriceIndicator(model.price_output);
                    priceIndicator.title = `$${model.price_input}/M in, $${model.price_output}/M out`;
                    indicators.appendChild(priceIndicator);
                }

                // Context indicator
                if (model.context_window !== undefined) {
                    const contextIndicator = document.createElement('span');
                    contextIndicator.className = 'model-indicator model-indicator-context';
                    contextIndicator.textContent = this.formatContextWindow(model.context_window);
                    contextIndicator.title = `${model.context_window.toLocaleString()} token context window`;
                    indicators.appendChild(contextIndicator);
                }

                option.appendChild(nameSpan);
                option.appendChild(indicators);

                // Pre-select default model
                if (model.model === this.capabilities.default_model) {
                    option.classList.add('selected');
                    this.selectedModel = model.model;
                    this.updateSelectedDisplay(selectedDisplay, model);
                    matchFound = true;
                }

                // Click handler
                option.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.selectModel(model.model, selectedDisplay, optionsContainer);
                    customDropdown.classList.remove('open');
                });

                optionsContainer.appendChild(option);
            });
        });

        // Fallback if default not found
        if (!matchFound && firstModel) {
            logger.warn('[ConfigPanel] Default model not found in capabilities, falling back to:', firstModel);
            this.selectedModel = firstModel;
            const firstModelData = this.modelData[firstModel];
            this.updateSelectedDisplay(selectedDisplay, firstModelData);
            optionsContainer.querySelector('.model-dropdown-option').classList.add('selected');
        }

        customDropdown.appendChild(selectedDisplay);
        customDropdown.appendChild(optionsContainer);
        selectContainer.appendChild(customDropdown);

        // Toggle dropdown on click
        selectedDisplay.addEventListener('click', () => {
            customDropdown.classList.toggle('open');
        });

        // Close on outside click
        document.addEventListener('click', (e) => {
            if (!customDropdown.contains(e.target)) {
                customDropdown.classList.remove('open');
            }
        });
    }

    getPriceIndicator(outputPrice) {
        // Return $ symbols based on price tier
        if (outputPrice <= 0.5) return '$';
        if (outputPrice <= 2) return '$$';
        if (outputPrice <= 10) return '$$$';
        if (outputPrice <= 30) return '$$$$';
        return '$$$$$';
    }

    formatContextWindow(tokens) {
        if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(0)}M`;
        if (tokens >= 100000) return `${(tokens / 1000).toFixed(0)}K`;
        return `${(tokens / 1000).toFixed(0)}K`;
    }

    updateSelectedDisplay(displayEl, model) {
        const textEl = displayEl.querySelector('.model-dropdown-text');
        if (!model) {
            textEl.textContent = 'Select model...';
            return;
        }

        // Create rich display for selected model
        let html = `<span class="selected-model-name">${model.model}</span>`;

        // Add indicators
        if (model.price_output !== undefined || model.context_window !== undefined) {
            html += '<span class="selected-model-indicators">';
            if (model.price_output !== undefined) {
                html += `<span class="model-indicator model-indicator-price">${this.getPriceIndicator(model.price_output)}</span>`;
            }
            if (model.context_window !== undefined) {
                html += `<span class="model-indicator model-indicator-context">${this.formatContextWindow(model.context_window)}</span>`;
            }
            html += '</span>';
        }

        textEl.innerHTML = html;
    }

    selectModel(modelName, displayEl, optionsContainer) {
        this.selectedModel = modelName;

        // Update native select (for form compatibility)
        this.modelSelect.value = modelName;

        // Update visual selection
        optionsContainer.querySelectorAll('.model-dropdown-option').forEach(opt => {
            opt.classList.toggle('selected', opt.dataset.value === modelName);
        });

        // Update display
        const modelData = this.modelData[modelName];
        this.updateSelectedDisplay(displayEl, modelData);
    }

    populateTools() {
        if (!this.toolsGroup || !this.capabilities.tools) return;

        // Clear existing tools
        this.toolsGroup.innerHTML = '';
        this.selectedTools = [];
        this.selectedMCPServers = [];

        // Create checkbox for each regular tool (exclude 'mcp' - we show individual servers instead)
        Object.entries(this.capabilities.tools).forEach(([toolName, toolInfo]) => {
            // Skip the generic 'mcp' tool - we show individual MCP servers below
            if (toolName === 'mcp') return;

            const isDefault = this.capabilities.default_tools.includes(toolName);
            const isInitialized = toolInfo.initialized !== false;  // Default to true if not specified

            const label = document.createElement('label');
            label.className = 'tool-checkbox-item';

            // Disable if tool failed to initialize
            if (!isInitialized) {
                label.classList.add('tool-disabled');
                label.title = `${toolName} failed to initialize`;
            }

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.value = toolName;
            checkbox.checked = isDefault && isInitialized;
            checkbox.disabled = !isInitialized;
            checkbox.dataset.type = 'tool';
            checkbox.addEventListener('change', () => this.updateSelections());

            const name = document.createElement('span');
            name.className = 'tool-checkbox-name';
            name.textContent = toolName;

            label.appendChild(checkbox);
            label.appendChild(name);
            this.toolsGroup.appendChild(label);

            if (isDefault && isInitialized) {
                this.selectedTools.push(toolName);
            }
        });

        // Add MCP servers as tool checkboxes (format: mcp:servername)
        const mcpServers = this.capabilities.mcp_servers || {};
        const globalServers = mcpServers.global || [];
        const userServers = mcpServers.user || [];

        // Add global MCP servers
        globalServers.forEach(server => {
            const label = document.createElement('label');
            label.className = 'tool-checkbox-item';

            // Check server status
            const status = server.status || 'unknown';
            const isConnected = status === 'connected';
            const hasError = status === 'error';

            // Disable if server failed to connect
            if (hasError) {
                label.classList.add('tool-disabled');
                label.title = server.error || 'Server failed to connect';
            }

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.value = server.tool_id;  // e.g., "mcp:anysite"
            checkbox.checked = isConnected;  // Default enabled if connected
            checkbox.disabled = hasError;
            checkbox.dataset.type = 'mcp';
            checkbox.addEventListener('change', () => this.updateSelections());

            const name = document.createElement('span');
            name.className = 'tool-checkbox-name';
            name.textContent = server.tool_id;  // Show as "mcp:anysite"

            // Add status indicator dot
            const statusDot = document.createElement('span');
            statusDot.className = `tool-status-indicator status-${status}`;
            statusDot.title = status === 'connected'
                ? `Connected (${server.tools_count || 0} tools)`
                : (server.error || status);

            // Add tools count if connected
            if (isConnected && server.tools_count > 0) {
                const toolsCount = document.createElement('span');
                toolsCount.className = 'mcp-tools-count';
                toolsCount.textContent = `(${server.tools_count})`;
                name.appendChild(toolsCount);
            }

            label.appendChild(checkbox);
            label.appendChild(name);
            label.appendChild(statusDot);
            this.toolsGroup.appendChild(label);

            if (checkbox.checked) {
                this.selectedMCPServers.push(server.tool_id);
            }
        });

        // Add user MCP servers
        userServers.forEach(server => {
            const label = document.createElement('label');
            label.className = 'tool-checkbox-item';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.value = server.tool_id;  // e.g., "mcp:user:myserver"
            checkbox.checked = server.enabled !== false;
            checkbox.dataset.type = 'mcp';
            checkbox.addEventListener('change', () => this.updateSelections());

            const name = document.createElement('span');
            name.className = 'tool-checkbox-name';
            name.textContent = server.tool_id;  // Show as "mcp:user:myserver"

            label.appendChild(checkbox);
            label.appendChild(name);
            this.toolsGroup.appendChild(label);

            if (checkbox.checked) {
                this.selectedMCPServers.push(server.tool_id);
            }
        });

        logger.debug('[ConfigPanel] Initial selection - Tools:', this.selectedTools, 'MCP:', this.selectedMCPServers);
    }

    updateSelections() {
        // Separate tools and MCP servers based on data-type attribute
        const checkboxes = this.toolsGroup.querySelectorAll('input[type="checkbox"]');

        this.selectedTools = [];
        this.selectedMCPServers = [];

        checkboxes.forEach(cb => {
            if (cb.checked) {
                if (cb.dataset.type === 'mcp') {
                    this.selectedMCPServers.push(cb.value);
                } else {
                    this.selectedTools.push(cb.value);
                }
            }
        });

        logger.debug('[ConfigPanel] Selection updated - Tools:', this.selectedTools, 'MCP:', this.selectedMCPServers);
    }

    getConfiguration() {
        // Validate configuration before returning
        const errors = [];

        if (!this.selectedModel) {
            errors.push('No model selected');
        }

        if (!Array.isArray(this.selectedTools) || this.selectedTools.length === 0) {
            errors.push('At least one tool must be selected');
        }

        if (typeof this.maxSteps !== 'number' || this.maxSteps < 1 || this.maxSteps > 200) {
            errors.push('Max steps must be between 1 and 200');
        }

        if (errors.length > 0) {
            this.showError(errors.join('. '));
            throw new Error(`Invalid configuration: ${errors.join('. ')}`);
        }

        // Return validated configuration with MCP servers
        // Vision and temperature are handled automatically by the backend
        return {
            model: this.selectedModel,
            tools: this.selectedTools,
            mcp_servers: this.selectedMCPServers,  // Include selected MCP servers
            max_steps: this.maxSteps
        };
    }

    showError(message) {
        logger.error('[ConfigPanel]', message);

        // Create or get error element
        let errorEl = this.panel.querySelector('.config-error');
        if (!errorEl) {
            errorEl = document.createElement('div');
            errorEl.className = 'config-error';
            this.panel.insertBefore(errorEl, this.panel.firstChild);
        }

        errorEl.textContent = `⚠️ ${message}`;
        errorEl.style.display = 'block';

        // Auto-hide after 5 seconds
        setTimeout(() => {
            if (errorEl) {
                errorEl.style.display = 'none';
            }
        }, 5000);
    }

    hide() {
        if (this.panel) {
            this.panel.style.display = 'none';
        }
    }
}

// Initialize config panel when DOM is ready
let configPanel = null;

document.addEventListener('DOMContentLoaded', () => {
    // Use centralized session state
    const isNewSession = sessionState.isNew;

    logger.debug('[ConfigPanel] Init check - isNew:', isNewSession);

    if (isNewSession) {
        logger.debug('[ConfigPanel] Initializing for new session');
        configPanel = new ConfigPanel();
        // Make globally available
        window.configPanel = configPanel;
    }
});

export { configPanel };
