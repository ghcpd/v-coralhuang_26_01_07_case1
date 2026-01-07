import audit_log_service
import enhanced_audit_service

audit_log_service.init()
enhanced_audit_service.init()

q = {'from_ts': 1767147805, 'to_ts': 1767752605, 'actor_id': None, 'action': 'DELETE', 'page': 505, 'page_size': 10}
start, end = enhanced_audit_service._find_created_range(q['from_ts'], q['to_ts'])
print('start, end =', start, end)
if start < len(enhanced_audit_service._created_at_list):
    print('created_at[start]:', enhanced_audit_service._created_at_list[start])
if end-1 >= 0 and end-1 < len(enhanced_audit_service._created_at_list):
    print('created_at[end-1]:', enhanced_audit_service._created_at_list[end-1])
print('len list:', len(enhanced_audit_service._created_at_list))

# Show first 10 entries in range (or indicate empty)
for i in range(start, min(start+10, end)):
    print(i, enhanced_audit_service._created_at_list[i])

# If start >= end show that it's empty
if start >= end:
    print('Empty range as expected')
else:
    print('Range non-empty')
