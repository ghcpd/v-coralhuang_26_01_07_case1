import audit_log_service
import enhanced_audit_service

audit_log_service.init()
enhanced_audit_service.init()

for i in range(1000):
    b = audit_log_service.handle_request()
    q = b['query']
    e = enhanced_audit_service.query_with_params(q)
    if b['count'] != e['count']:
        print('Mismatch at sample', i)
        print('query:', q)
        print('baseline count:', b['count'])
        print('enhanced count:', e['count'])
        print('baseline events:', b['events'])
        print('enhanced events:', e['events'])
        break
else:
    print('No mismatch found')
