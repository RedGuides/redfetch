// ==============================
// Shared Utilities and Initializations
// ==============================
document.addEventListener('DOMContentLoaded', initialize);

async function initialize() {
    const cookieName = 'using_flask'; 
    const cookieValue = getCookieValue(cookieName);

    if (cookieValue) {
        try {
            const [allowedCategoryIds, specialResourceIds] = await Promise.all([
                fetchCategoryIds(),
                fetchSpecialResourceIds()
            ]);
            setupSingleResourceInstallButton();
            setupUpdateWatchedButton();
            initializeCookieButtons(allowedCategoryIds, specialResourceIds);
        } catch (error) {
            console.error('Initialization failed:', error);
        }
    } else {
        console.log("Required cookie not found. Initialization halted.");
    }
}

function getCookieValue(cookieName) {
    const cookie = document.cookie.split('; ').find(row => row.startsWith(cookieName + '='));
    return cookie ? cookie.split('=')[1] : null;
}

function initializeCookieButtons(allowedCategoryIds, specialResourceIds) {
    const cookieButtons = document.querySelectorAll('[data-xf-init="cookie-trigger"]');
    cookieButtons.forEach(button => {
        const cookieName = button.getAttribute('data-cookie-name');
        const cookieValue = getCookieValue(cookieName);
        
        if (cookieValue) {
            if (button.id === 'installButton') {
                const parentCategoryId = parseInt(button.getAttribute('data-parent-category-id'), 10);
                const resourceId = parseInt(button.getAttribute('data-resource-id'), 10);
                if (allowedCategoryIds.includes(parentCategoryId) || specialResourceIds.includes(resourceId)) {
                    checkLocalServerAndDisplayButton(button);
                }
            } else if (button.id === 'updateWatchedButton') {
                checkLocalServerAndDisplayButton(button);
            }
        } else {
            console.log("Cookie not found for button:", button.id);
        }
    });
}

async function checkLocalServerAndDisplayButton(button) {
    try {
        const response = await fetch('http://127.0.0.1:7734/health', { method: 'GET' });
        if (response.ok) {
            console.log("Server is running, displaying button.");
            button.style.display = 'inline-flex';
        } else {
            throw new Error('Server not responding');
        }
    } catch (error) {
        console.error('Error checking local server:', error);
    }
}

async function fetchCategoryIds() {
    const key = 'allowedCategoryIds';
    return fetchIdsFromStorageOrServer(key, 'http://127.0.0.1:7734/category-map');
}

async function fetchSpecialResourceIds() {
    const key = 'specialResourceIds';
    return fetchIdsFromStorageOrServer(key, 'http://127.0.0.1:7734/special-resource-ids');
}

// fetches the ids. 
async function fetchIdsFromStorageOrServer(storageKey, url) {
    let ids = sessionStorage.getItem(storageKey);
    if (ids) {
        return JSON.parse(ids);
    } else {
        const response = await fetch(url, { method: 'GET' });
        const data = await response.json();
        console.log(`Fetched ${storageKey}:`, data);
        sessionStorage.setItem(storageKey, JSON.stringify(data));
        return data;
    }
}

async function fetchDataFromServer(url, options) {
    try {
        const response = await fetch(url, options);
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.message || 'Failed to fetch data from server.');
        }
        return data;
    } catch (error) {
        console.error('Fetch error:', error);
        throw error;  // Rethrow to handle in specific contexts
    }
}

function startButtonIconAnimation(button, icons) {
    let iconIndex = 0;
    button.innerHTML = `<i class="fa--xf far fa-${icons[iconIndex]}"></i> Updating...`;
    return setInterval(() => {
        iconIndex = (iconIndex + 1) % icons.length;
        button.innerHTML = `<i class="fa--xf far fa-${icons[iconIndex]}"></i> Updating...`;
    }, 400);
}

function handleButtonError(button, iconInterval, errorMessage) {
    clearInterval(iconInterval);
    alert('Error: ' + errorMessage);
    button.innerHTML = '<i class="fa--xf far fa-skull-crossbones"></i> Error';
    setTimeout(() => resetButton(button, 'Retry'), 5000); // Reset the button after 5 seconds
}

function resetButton(button, label = 'Retry') {
    button.innerHTML = `<i class="fa--xf far fa-redo-alt"></i> ${label}`;
    button.disabled = false;
    button.style.backgroundColor = ''; // Reset to default or specify a default color
    button.style.borderColor = '';     // Reset to default or specify a default color
    button.style.color = '';           // Reset to default or specify a default color
}

// ==============================
// Single Resource Button Logic
// ==============================

async function setupSingleResourceInstallButton() {
    const installButton = document.getElementById('installButton');
    if (!installButton) return;

    const resourceId = installButton.getAttribute('data-resource-id');
    const remoteVersion = installButton.getAttribute('data-remote-version');

    try {
        const response = await fetch(`http://127.0.0.1:7734/health?resource_id=${resourceId}&remote_version=${remoteVersion}`, { method: 'GET' });
        const data = await response.json();

        updateSingleResourceButtonBasedOnAction(installButton, data.action);
        installButton.onclick = () => handleSingleResourceButtonClick(data.action, resourceId);
    } catch (error) {
        console.error('Error fetching health status:', error);
        installButton.innerHTML = '<i class="fa--xf far fa-exclamation-triangle"></i> Error';
    }
}

function updateSingleResourceButtonBasedOnAction(button, action) {
    const actions = {
        'update': '<i class="fa--xf far fa-sync-alt"></i> Update',
        'install': '<i class="fa--xf far fa-cabinet-filing"></i> Install',
        're-install': '<i class="fa--xf far fa-redo-alt"></i> Re-install'
    };
    button.innerHTML = actions[action] || 'Unknown Action';
}

function handleSingleResourceButtonClick(action, resourceId) {
    if (action === "re-install") {
        resetAndReinstallSingleResource(resourceId);
    } else {
        startSingleResourceInstallation(document.getElementById('installButton'), resourceId);
    }
}

async function resetAndReinstallSingleResource(resourceId) {
    const installButton = document.getElementById('installButton');
    try {
        const data = await fetchDataFromServer('http://127.0.0.1:7734/reset-download-date', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resource_id: resourceId })
        });
        startSingleResourceInstallation(installButton, resourceId);
    } catch (error) {
        handleButtonError(installButton, null, error.message); // No iconInterval in this context
    }
}

async function startSingleResourceInstallation(installButton, resourceId) {
    const icons = ['dragon', 'mace', 'scroll-old', 'hat-wizard', 'mandolin', 'shield-cross', 'staff', 'paw-claws', 'flask-potion', 'campfire', 'dagger', 'sword', 'treasure-chest', 'axe', 'ring', 'bow-arrow', 'hood-cloak', 'scythe', 'wand-magic', 'hammer-war', 'book-spells', 'cauldron', 'hand-sparkles'];
    let iconInterval = startButtonIconAnimation(installButton, icons);

    try {
        const data = await fetchDataFromServer('http://127.0.0.1:7734/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resource_id: resourceId })
        });
        clearInterval(iconInterval); // Stop the animation
        updateSingleResourceButtonSuccess(installButton);
    } catch (error) {
        handleButtonError(installButton, iconInterval, error.message);
    }
}

function updateSingleResourceButtonSuccess(installButton, nextAction = 'Re-install') {
    installButton.innerHTML = '<i class="fa--xf far fa-crown"></i>';
    installButton.style.backgroundColor = 'green';
    installButton.style.borderColor = 'green';
    installButton.style.color = 'white';
    setTimeout(() => resetButton(installButton, nextAction), 5000);
}

// ==============================
// Watched Resources Button Logic
// ==============================

function setupUpdateWatchedButton() {
    const updateWatchedButton = document.getElementById('updateWatchedButton');
    if (!updateWatchedButton) return;

    const icons = ['dragon', 'mace', 'scroll-old', 'hat-wizard', 'mandolin', 'shield-cross', 'staff', 'paw-claws', 'flask-potion', 'campfire', 'dagger', 'sword', 'treasure-chest', 'axe', 'ring', 'bow-arrow', 'hood-cloak', 'scythe', 'wand-magic', 'hammer-war', 'book-spells', 'cauldron', 'hand-sparkles'];

    updateWatchedButton.onclick = async () => {
        updateWatchedButton.disabled = true;
        const iconInterval = startButtonIconAnimation(updateWatchedButton, icons);
        await updateWatchedResources(updateWatchedButton, iconInterval);
    };
}

async function updateWatchedResources(button, iconInterval) {
    try {
        const data = await fetchDataFromServer('http://127.0.0.1:7734/download-watched', { method: 'POST' });
        clearInterval(iconInterval); // Stop the animation
        console.log('Watched resources updated:', data);
        alert('Watched resources updated successfully.');
        button.innerHTML = '<i class="fa--xf far fa-crown"></i> Updated';
    } catch (error) {
        handleButtonError(button, iconInterval, error.message);
    } 
}

