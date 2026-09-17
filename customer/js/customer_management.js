document.addEventListener('DOMContentLoaded', ()=>{

    const customerTable = document.querySelector('#customerTable');

    // ===== Add Customer =====
    const addCustomerBtn = document.querySelector('#user-management button[style*="background: #16a34a;"]');
    if(addCustomerBtn) addCustomerBtn.addEventListener('click', () => {
        const username = prompt("Enter Customer Username:");
        const email = prompt("Enter Customer Email:");
        const phone = prompt("Enter Phone Number:");
        const address = prompt("Enter Address:");
        const customer_type = prompt("Enter Customer Type (Regular/Senior/PWD):");
        const password = prompt("Enter password for this user:");

        if(username && email && phone && address && customer_type && password){
            sendUserAction({
                action: 'add',
                username,
                email,
                phone,
                address,
                customer_type,
                password
            }, res=>{
                if(res.status==='success'){
                    const newRow = document.createElement('tr');
                    newRow.dataset.id = res.user_id;
                    newRow.innerHTML = `
                        <td>${username}</td>
                        <td>${email}</td>
                        <td>${phone}</td>
                        <td>${address}</td>
                        <td>${customer_type}</td>
                        <td class="action-btn-group">
                            <button class="edit-btn" style="background: #1e90ff; padding: 6px 12px;">Edit</button>
                            <button class="delete-btn" style="background: #e63946; padding: 6px 12px;">Delete</button>
                        </td>
                    `;
                    customerTable.appendChild(newRow);
                    attachEditEvents(customerTable);
                    attachDeleteEvents(customerTable);
                    alert("Customer added successfully!");
                } else alert(res.msg);
            });
        } else alert("All fields are required!");
    });

    // ===== Edit User =====
    function attachEditEvents(table){
        table.querySelectorAll('.edit-btn').forEach(btn=>{
            btn.onclick = (e)=>{
                const row = e.target.closest('tr');
                if(!row) return;
                const user_id = row.dataset.id;

                const currentUsername = row.cells[0].innerText.trim();
                const currentEmail = row.cells[1].innerText.trim();
                const currentPhone = row.cells[2].innerText.trim();
                const currentAddress = row.cells[3].innerText.trim();
                const currentType = row.cells[4].innerText.trim();

                const username = prompt("Edit Username:", currentUsername);
                if(username===null) return;
                const email = prompt("Edit Email:", currentEmail);
                if(email===null) return;
                const phone = prompt("Edit Phone Number:", currentPhone);
                if(phone===null) return;
                const address = prompt("Edit Address:", currentAddress);
                if(address===null) return;
                const customer_type = prompt("Edit Customer Type:", currentType);
                if(customer_type===null) return;
                const password = prompt("Enter new password (leave blank if not changing):", "");

                sendUserAction({
                    action: 'edit',
                    user_id,
                    username,
                    email,
                    phone,
                    address,
                    customer_type,
                    password
                }, res=>{
                    if(res.status==='success'){
                        row.cells[0].innerText = username;
                        row.cells[1].innerText = email;
                        row.cells[2].innerText = phone;
                        row.cells[3].innerText = address;
                        row.cells[4].innerText = customer_type;
                        alert("Customer updated successfully!");
                    } else alert(res.msg);
                });
            };
        });
    }

    // ===== Delete User =====
    function attachDeleteEvents(table){
        table.querySelectorAll('.delete-btn').forEach(btn=>{
            btn.onclick = (e)=>{
                if(!confirm("Are you sure you want to delete this customer?")) return;
                const row = e.target.closest('tr');
                if(!row) return;
                const user_id = row.dataset.id;

                sendUserAction({action:'delete', user_id}, res=>{
                    if(res.status==='success'){
                        row.remove();
                        alert("Customer deleted successfully!");
                    } else alert(res.msg);
                });
            };
        });
    }

    // ===== Initialize =====
    attachEditEvents(customerTable);
    attachDeleteEvents(customerTable);

    // ===== Observe new rows =====
    const observer = new MutationObserver(() => {
        attachEditEvents(customerTable);
        attachDeleteEvents(customerTable);
    });
    observer.observe(customerTable, {childList:true});
});

// ===== AJAX function =====
function sendUserAction(data, callback){
    fetch('/api/admin/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify(data)
    })
    .then(res=>res.json())
    .then(callback)
    .catch(err=>alert("Error: "+err));
}
