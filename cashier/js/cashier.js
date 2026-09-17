cashier.js


let cart = [];
let editIndex = null;

const inventoryBody = document.getElementById("inventoryBody");
const posBody = document.getElementById("posBody");
const totalAmount = document.getElementById("totalAmount");

function loadInventory() {
  inventoryBody.innerHTML = "";
  const today = new Date();

  inventory.forEach((item, index) => {
    const expiryDate = new Date(item.expiry);
    const daysToExpire = Math.ceil((expiryDate - today) / (1000 * 60 * 60 * 24));

    let rowClass = "";
    let alertText = "";

    if (daysToExpire < 0) {
      rowClass = "expired";
      alertText = " (Expired)";
    } else if (daysToExpire <= 30) {
      rowClass = "expiring";
      alertText = " (Expiring Soon)";
    } else if (item.stock <= 5) {
      rowClass = "low-stock";
      alertText = " (Low Stock)";
    }

    inventoryBody.innerHTML += `
      <tr class="${rowClass}">
        <td>${item.name}${alertText}</td>
        <td>₱${item.price.toFixed(2)}</td>
        <td>${item.stock}</td>
        <td>${item.expiry}</td>
        
        <td>
          <button onclick="addToCart(${index})">➕</button>
          <button onclick="editItem(${index})">✏️</button>
          <button onclick="deleteItem(${index})">🗑️</button>
        </td>
      </tr>`;
  });
}

function addToCart(index) {
  const item = inventory[index];
  const today = new Date(item.expiry);
  if (item.stock <= 0) return alert("Out of stock!");
  if (new Date(item.expiry) < new Date()) return alert("Cannot sell expired medicine!");

  const existing = cart.find(i => i.name === item.name);
  if (existing) existing.qty++;
  else cart.push({ ...item, qty: 1 });
  item.stock--;
  loadInventory();
  updateCart();
}

function updateCart() {
  posBody.innerHTML = "";
  let total = 0;
  cart.forEach(item => {
    const subtotal = item.price * item.qty;
    total += subtotal;
    posBody.innerHTML += `
      <tr>
        <td>${item.name}</td>
        <td>${item.qty}</td>
        <td>₱${subtotal.toFixed(2)}</td>
        <td><button onclick="removeItem('${item.name}')">❌</button></td>
      </tr>`;
  });
  totalAmount.textContent = total.toFixed(2);
}

function removeItem(name) {
  const index = cart.findIndex(i => i.name === name);
  if (index !== -1) {
    const item = cart[index];
    const invItem = inventory.find(i => i.name === name);
    invItem.stock += item.qty;
    cart.splice(index, 1);
    loadInventory();
    updateCart();
  }
}

document.getElementById("checkoutBtn").addEventListener("click", () => {
  if (cart.length === 0) return alert("Cart is empty!");
  alert("✅ Transaction completed!");
  cart = [];
  updateCart();
});

document.getElementById("logoutBtn").addEventListener("click", () => {
  window.location.href = "/api/auth/logout";
});

/* --- Inventory Modal Logic --- */
const modal = document.getElementById("inventoryModal");
const addBtn = document.getElementById("addItemBtn");
const closeModal = document.getElementById("closeModalBtn");
const saveBtn = document.getElementById("saveItemBtn");
const nameInput = document.getElementById("medicineName");
const priceInput = document.getElementById("medicinePrice");
const stockInput = document.getElementById("medicineStock");
const expiryInput = document.getElementById("medicineExpiry");

addBtn.onclick = () => {
  editIndex = null;
  document.getElementById("modalTitle").textContent = "Add Medicine";
  modal.style.display = "flex";
  nameInput.value = "";
  priceInput.value = "";
  stockInput.value = "";
  expiryInput.value = "";
};

closeModal.onclick = () => (modal.style.display = "none");

saveBtn.onclick = () => {
  const name = nameInput.value.trim();
  const price = parseFloat(priceInput.value);
  const stock = parseInt(stockInput.value);
  const expiry = expiryInput.value;

  if (!name || isNaN(price) || isNaN(stock) || !expiry) return alert("Please fill all fields.");

  if (editIndex !== null) {
    inventory[editIndex] = { name, price, stock, expiry };
  } else {
    inventory.push({ name, price, stock, expiry });
  }

  modal.style.display = "none";
  loadInventory();
};

function editItem(index) {
  editIndex = index;
  document.getElementById("modalTitle").textContent = "Edit Medicine";
  const item = inventory[index];
  nameInput.value = item.name;
  priceInput.value = item.price;
  stockInput.value = item.stock;
  expiryInput.value = item.expiry;
  modal.style.display = "flex";
}

function loadPOSInventory() {
    const posBody = document.getElementById("posInventoryBody");
    posBody.innerHTML = "";

    inventory.forEach((item, index) => {
        posBody.innerHTML += `
            <tr data-category="${item.category}">
                <td>${item.brand}</td>
                <td>${item.generic}</td>
                <td>${item.dosage}</td>
                <td>${item.form}</td>
            </tr>
        `;
    });
}

function deleteItem(index) {
  if (confirm("Delete this item?")) {
    inventory.splice(index, 1);
    loadInventory();
  }
}

// Function para kunin at i-display ang categories
function loadCategories() {
    fetch('/api/cashier/categories')
    .then(response => response.json())
    .then(categories => {

        const filterSelect = document.getElementById('categoryFilter');

        if (!filterSelect) {
            console.warn("Category dropdown not ready yet. Retrying in 200ms...");
            setTimeout(loadCategories, 200);
            return;
        }

        let optionsHTML = '<option value="">All Categories</option>';

        categories.forEach(category => {
            optionsHTML += `<option value="${category}">${category}</option>`;
        });

        filterSelect.innerHTML = optionsHTML;

        console.log("Categories loaded:", categories);
    })
    .catch(error => console.error("Error fetching categories:", error));
}


// Tawagin ang function kapag nag-load ang page
document.addEventListener('DOMContentLoaded', function() {

// O kung gumagamit ka ng jQuery:
// $(document).ready(loadCategories);

loadInventory();

});

document.querySelectorAll('.nav-item').forEach(nav => {
    nav.addEventListener('click', function () {
        let target = this.dataset.page;

        // If POS page is opened, load categories
if (target === "pos-page") {
    console.log("POS page activated → Loading categories & POS inventory...");
    loadCategories();
    loadPOSInventory();

        }
    });
});