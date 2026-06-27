"""
Basecamp Recon — research tools for analysing the Basecamp multi-arm run.

This package is OFFLINE research only: it reads stored depth, never touches the
live trader. The centrepiece is the regime/trend lens — a constant-velocity
Kalman filter + a variance-ratio/Hurst regime classifier — used to pre-test the
B1 hypothesis ("does the maker excel in orderly trends, not chop?").
"""
